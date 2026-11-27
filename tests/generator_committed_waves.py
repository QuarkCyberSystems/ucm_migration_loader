"""Exercise sealed first and subsequent waves, including a live caseworker on the first."""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
from contextlib import ExitStack
from itertools import islice
from pathlib import Path
from time import monotonic
from uuid import uuid4

import frappe
from generator_snapshot import BENCH
from rehearsal_bench import login_caseworker, summary, tls_site
from requests import HTTPError, RequestException

from ucm_migration_loader.frozen import verify_publication
from ucm_migration_loader.publication_receiver import local_publication


def invoke(site, method, run, **kwargs):
	command = [
		"bench",
		"--site",
		site,
		"execute",
		"ucm_migration_loader.publication_worker." + method,
		"--kwargs",
		json.dumps({"run": run, **kwargs}),
	]
	proc = subprocess.run(command, cwd=BENCH, text=True, capture_output=True, check=False)
	if proc.returncode:
		raise RuntimeError(proc.stderr + proc.stdout)
	return json.loads(proc.stdout)


def fixture(site, run):
	path = local_publication(run)
	manifest = verify_publication(path, run)
	assert manifest["site"] == site
	with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
		for key, value in db.execute("select id,payload from refs where dt='User'"):
			if frappe.db.exists("User", key):
				continue
			actor = json.loads(value)
			assert key.endswith("@example.org") and actor["email"] == key
			frappe.get_doc(
				{
					"doctype": "User",
					"email": key,
					"first_name": actor["full_name"],
					"enabled": 0,
					"user_type": "Website User",
					"send_welcome_email": 0,
				}
			).insert()
			assert not frappe.db.get_value("User", key, "enabled")
	frappe.db.commit()
	return manifest


def exercise(
	site,
	first,
	second,
	workspace,
	caseworkers=1,
	test_held_shares=False,
	*,
	require_error_free=True,
):
	if caseworkers < 1:
		raise ValueError("Use a positive number of caseworkers")
	from ucm.tests.utils import make_user

	assert site.startswith("ucm-full-wave-") and site.endswith(".localhost")
	a, b = fixture(site, first), fixture(site, second)

	def state(run):
		frappe.db.rollback()
		return frappe.get_doc("UCM Migration Run", run).state

	durations = {}
	started = monotonic()
	if state(first) != "Activated":
		invoke(site, "apply", first, max_batches=a["batches"])
	durations["first_apply"] = round(monotonic() - started, 2)
	started = monotonic()
	first_receipt = invoke(site, "activate", first)
	durations["first_activate"] = round(monotonic() - started, 2)
	assert first_receipt["applied"] == a["batches"] and first_receipt["state"] == "Activated"
	assert state(second) != "Activated"
	users = []
	with sqlite3.connect(local_publication(first).resolve().as_uri() + "?mode=ro", uri=True) as publication:
		eligible = (
			r["row"]["name"]
			for (blob,) in publication.execute("select payload from batches order by sequence")
			for r in json.loads(blob)
			if r["doctype"] == "Case" and not r["row"].get("case_tags")
		)

		cases = list(islice(eligible, caseworkers))
	if len(cases) != caseworkers:
		raise ValueError("The first wave needs a distinct accessible case for each caseworker")

	phase = "baseline"
	stop = threading.Event()
	samples = [[] for _ in cases]
	last_save = [None for _ in cases]
	access_checks = {}
	try:
		with ExitStack() as stack:
			url, cert = stack.enter_context(tls_site(site, workspace, threaded=True))
			sessions = []
			for _ in cases:
				user = make_user(f"wave-{uuid4().hex}@example.invalid", [(a["context"], "Write")])
				users.append(user)
				frappe.db.commit()
				sessions.append(stack.enter_context(login_caseworker(user, url, cert)))
			session_lock = threading.Lock()

			def request(method, path, **kwargs):
				with session_lock:
					return sessions[0](method, path, **kwargs)

			if test_held_shares:
				import generator_held_shares
				from generator_caseworker_surfaces import check as check_surfaces

				recipient_user = make_user(
					f"receiver-{uuid4().hex}@example.invalid", [(b["context"], "Read")]
				)
				users.append(recipient_user)
				frappe.db.commit()
				recipient_request = stack.enter_context(login_caseworker(recipient_user, url, cert))
				held_examples = generator_held_shares.before(first, b["context"], request, recipient_request)

			def invisible(name):
				data = request(
					"GET",
					"/api/resource/Case",
					params={"filters": json.dumps({"name": name})},
				)["data"]
				assert data == []
				try:
					request("GET", "/api/resource/Case/" + name)
				except HTTPError as exc:
					assert exc.response.status_code in (403, 404)
				else:
					raise AssertionError("Unshared case was readable")

			code = frappe.db.get_value("Context", a["context"], "context_code")
			identifiers = []
			for dt, label, field in (
				("Case", "case_name", "case_number"),
				("Person", "person_name", "person_file_id"),
			):
				created = request(
					"POST",
					"/api/resource/" + dt,
					json={
						"context_tech_id": a["context"],
						label: "First wave rehearsal",
					},
				)["data"]
				identifier = created[field]
				assert re.fullmatch(rf"{re.escape(code)}-\d{{8}}-\d{{4}}-[1-9]\d*", identifier)
				# The HTTP request committed on a different MariaDB connection.
				frappe.db.rollback()
				assert frappe.db.count(dt, {field: identifier}) == 1
				identifiers.append(identifier)
			assert len(set(identifiers)) == 2

			def traffic(index):
				send = sessions[index] if index else request
				case = cases[index]
				while not stop.is_set():
					for method, operation, params in (
						("GET", "read", {}),
						(
							"PUT",
							"save",
							{"json": {"case_remark": "Generated wave rehearsal " + str(len(samples[index]))}},
						),
					):
						started, label = monotonic(), phase
						try:
							doc = send(method, "/api/resource/Case/" + case, **params)["data"]
							ok = doc["name"] == case and (
								method == "GET" or doc["case_remark"] == params["json"]["case_remark"]
							)
							if ok and method == "PUT":
								last_save[index] = doc["case_remark"]
						except RequestException:
							ok = False
						samples[index].append((label, operation, 1000 * (monotonic() - started), ok))
					stop.wait(0.2)

			clients = [threading.Thread(target=traffic, args=(i,)) for i in range(len(cases))]
			for client in clients:
				client.start()
			try:
				stop.wait(2)
				phase, started = "apply", monotonic()
				checkpoint = invoke(site, "apply", second, max_batches=1)
				held = invoke(site, "apply", second, max_batches=b["batches"])
				durations["second_apply"] = round(monotonic() - started, 2)
				phase = "hidden_access_checks"
				assert held["pending_activation_from_batch"] == held["applied"] + 1
				assert state(second) == "Publication ready"
				assert not frappe.db.exists("Record Share", {"context_tech_id": b["context"]})
				with sqlite3.connect(
					local_publication(second).resolve().as_uri() + "?mode=ro", uri=True
				) as sealed:
					case_names, shared = [], set()
					for (payload,) in sealed.execute("select payload from batches order by sequence"):
						for item in json.loads(payload):
							row = item["row"]
							if item["doctype"] == "Case":
								case_names.append(row["name"])
							if item["doctype"] == "Record Share" and row["share_doctype"] == "Case":
								shared.add(row["share_name"])
					hidden_case = next(name for name in case_names if name not in shared)
				assert frappe.db.exists("Case", hidden_case)
				invisible(hidden_case)
				if test_held_shares:
					access_checks["while_hidden"] = check_surfaces(request, first, second)
				phase, started = "activate", monotonic()
				second_receipt = invoke(site, "activate", second)
				durations["second_activate"] = round(monotonic() - started, 2)
				phase = "released_access_checks"
				assert second_receipt["state"] == "Activated" and second_receipt["applied"] == b["batches"]
				assert not frappe.db.exists(
					"Record Share",
					{
						"share_doctype": "Case",
						"share_name": hidden_case,
						"share_to_context_tech_id": a["context"],
					},
				)
				invisible(hidden_case)
				if test_held_shares:
					access_checks["after_activation"] = check_surfaces(request, first, second)
					access_checks["historical_shares"] = generator_held_shares.after(
						first, second, held_examples, recipient_request
					)
			finally:
				stop.set()
				for client in clients:
					client.join()
			for i, case in enumerate(cases):
				assert (
					last_save[i]
					and sessions[i]("GET", "/api/resource/Case/" + case)["data"]["case_remark"]
					== last_save[i]
				)
		per_user = [summary(rows) for rows in samples]
		for user_result in per_user:
			assert all(
				user_result[p][op]["requests"]
				and (not require_error_free or user_result[p][op]["errors"] == 0)
				for p in ("baseline", "apply", "activate")
				for op in ("read", "save")
			), user_result
		metrics = summary([sample for rows in samples for sample in rows])
		assert all(
			metrics[p][op]["requests"] and (not require_error_free or metrics[p][op]["errors"] == 0)
			for p in ("baseline", "apply", "activate")
			for op in ("read", "save")
		), metrics
		assert invoke(site, "activate", second) == second_receipt
		return {
			"site": site,
			"timings_seconds": durations,
			"access_checks": access_checks,
			"first": first_receipt,
			"second_checkpoint": checkpoint,
			"second_hidden": held,
			"second": second_receipt,
			"caseworker": metrics,
			"concurrent_caseworkers": caseworkers,
			"per_caseworker": per_user,
			"all_last_saves_verified": True,
			"first_case_and_person_identifiers_unique": True,
			"unshared_case_denied_during_and_after_wave": True,
		}
	finally:
		frappe.db.rollback()
		for user in users:
			account = frappe.get_doc("User", user)
			account.enabled = 0
			account.save()
		frappe.db.commit()


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description=__doc__)
	for arg in ("site", "first", "second", "evidence"):
		parser.add_argument("--" + arg, required=True)
	parser.add_argument("--caseworkers", type=int, default=1)
	parser.add_argument("--test-held-shares", action="store_true")
	args = parser.parse_args()
	output = Path(args.evidence).resolve()
	assert not output.exists()
	os.chdir(BENCH / "sites")
	frappe.init(site=args.site)
	frappe.connect()
	try:
		with tempfile.TemporaryDirectory() as work:
			result = exercise(
				args.site,
				args.first,
				args.second,
				Path(work),
				args.caseworkers,
				args.test_held_shares,
			)
		with output.open("x") as handle:
			json.dump(result, handle, indent=2)
		print(json.dumps(result))
	finally:
		frappe.destroy()
