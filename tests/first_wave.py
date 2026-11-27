"""Exercise first-delegation cancellation, dictionary preflight and cutover on a fresh UCM site."""

import argparse
import json
import os
import re
import secrets
import tempfile
from contextlib import contextmanager
from pathlib import Path
from time import monotonic
from uuid import uuid4

import frappe
import pymysql
import requests
from rehearse import call, new_context, worker
from transport import BENCH, publication_db, tls_site, transfer

from ucm_migration_loader import api
from ucm_migration_loader.contract import plan
from ucm_migration_loader.push import publication, publisher_schema, send

PILOT_TABLES = ("Person", "Person Other Name", "Case", "Case Member")


def counts():
	return {dt: frappe.db.count(dt) for dt in PILOT_TABLES}


def require_fresh_site():
	from ucm.utils.data_model import context_scopes

	if frappe.db.count("Context") or frappe.db.count(api.RUN) or any(counts().values()):
		raise ValueError(
			"First-wave qualification requires a fresh site with no contexts, runs or pilot rows"
		)
	for dt, scope in context_scopes().items():
		filters = {"context_tech_id": ["is", "set"]} if scope == "optional" else {}
		if frappe.db.count(dt, filters):
			raise ValueError(f"Fresh-site check found delegation data in {dt}")


def exercise_cancellation(site, context, ids, schema, config, member_role, caseworker):
	with pymysql.connect(**json.loads(config.read_text())) as conn:
		manifest, batches = plan(site, schema, context, publication(conn, context), size=1)
		manifest["schema"] = publisher_schema(conn)
	partial = send(call, manifest, batches, max_batches=1)
	assert partial["received"] == 1 and partial["state"] == "Receiving"
	run = partial["run"]
	assert call("discard", run=run)["state"] == "Discarded"
	receipt = frappe.db.get_value(api.BATCH, f"{run}-1", ["digest", "payload"])
	assert receipt == (manifest["batches"][0], None)
	assert not any(counts().values())
	assert frappe.db.exists("User", caseworker)
	assert frappe.db.exists("Team Security Profile", {"context_tech_id": context})

	missing = send(call, {**manifest, "attempt": 2}, batches)
	call("verify", run=missing["run"])
	worker()
	result = call("status", run=missing["run"])
	assert result["state"] == "Rejected", result
	assert f"dictionary preflight, batch 3, Case Member {ids['member']}" in result["finding"], result
	assert not any(counts().values())
	assert frappe.db.get_value("Context", context, "context_status") == "Locked"
	frappe.set_user(caseworker)
	try:
		assert not frappe.get_list("Case", filters={"name": ids["case"]})
	finally:
		frappe.set_user("Administrator")
	call("discard", run=missing["run"])
	frappe.get_doc(
		{
			"doctype": "Concept Value",
			"concept_urn": "PersonMemberRole",
			"concept_value_urn": "PersonMemberRole:SyntheticFirstWave",
			"concept_value_name": "Synthetic case member",
			"concept_value_status": "Active",
		}
	).insert(set_name=member_role)
	frappe.db.commit()
	return run, missing["run"]


@contextmanager
def caseworker_session(caseworker, work):
	with (
		tls_site(frappe.local.site, work) as (url, cert),
		login_caseworker(caseworker, url, cert) as request,
	):
		yield request


@contextmanager
def login_caseworker(caseworker, url, cert):
	from frappe.utils.password import update_password

	password = secrets.token_urlsafe(36)
	update_password(caseworker, password)
	frappe.db.commit()
	with requests.Session() as session:
		session.trust_env = False
		session.verify = str(cert)

		def request(method, path, *, binary=False, **kwargs):
			result = session.request(method, url + path, timeout=15, **kwargs)
			result.raise_for_status()
			return result.content if binary else result.json()

		request("POST", "/api/method/login", json={"usr": caseworker, "pwd": password})
		assert request("GET", "/api/method/frappe.auth.get_logged_user")["message"] == caseworker
		try:
			yield request
		finally:
			request("POST", "/api/method/logout")


def first_user_writes(context, ids, activity_type, service, request):
	code = frappe.db.get_value("Context", context, "context_code")

	def create(doctype, **fields):
		return request(
			"POST",
			"/api/resource/" + doctype,
			json={"context_tech_id": context, **fields},
		)["data"]

	listed = request(
		"GET",
		"/api/resource/Case",
		params={"filters": json.dumps({"name": ids["case"]})},
	)
	assert listed["data"] == [{"name": ids["case"]}], listed["data"]
	imported = request(
		"PUT",
		"/api/resource/Case/" + ids["case"],
		json={"case_remark": "First caseworker save after activation"},
	)["data"]
	activity = create(
		"Activity",
		activity_name="First case follow-up",
		activity_type_tech_id=activity_type,
		service_type_tech_id=service,
	)
	connection = create(
		"Activity Case",
		activity_tech_id=activity["name"],
		case_tech_id=imported["name"],
	)
	case = create("Case", case_name="First new case")
	person = create("Person", person_name="First new person")
	frappe.db.rollback()
	numbers = case["case_number"], person["person_file_id"]
	assert len(set(numbers)) == 2 and all(
		re.fullmatch(rf"{code}-\d{{8}}-\d{{4}}-[1-9]\d*", n) for n in numbers
	)
	assert not set(numbers) & {
		imported["case_number"],
		frappe.db.get_value("Person", ids["person"], "person_file_id"),
	}
	assert frappe.db.get_value("Case", ids["case"], "case_remark") == imported["case_remark"]
	assert frappe.db.exists("Activity Case", connection["name"])
	return numbers


def later_wave(site, socket, work, live_case, request):
	ctx = new_context("second-wave")
	ids = {k: str(uuid4()) for k in ("person", "case", "other", "member")}
	frappe.db.commit()
	filters = {"filters": json.dumps({"name": ids["case"]})}
	remark = "Caseworker edited delegation A while delegation B was staged"

	def edit_while_staged():
		assert request("GET", "/api/resource/Case", params=filters)["data"] == []
		updated = request("PUT", "/api/resource/Case/" + live_case, json={"case_remark": remark})
		assert updated["data"]["case_remark"] == remark

	folder = work / "delegation-b"
	folder.mkdir()
	with publication_db(socket, ctx.name, ids, folder) as (schema, config):
		result = transfer(site, ctx.name, ids, folder, schema, config, on_staged=edit_while_staged)
	assert request("GET", "/api/resource/Case", params=filters)["data"] == []
	assert request("GET", "/api/resource/Case/" + live_case)["data"]["case_remark"] == remark
	return result


def run(site, socket, work):
	from ucm.tests.utils import make_activity_type, make_service, make_user

	require_fresh_site()
	started = monotonic()
	ctx = new_context("first-wave")
	ids = {k: str(uuid4()) for k in ("person", "case", "other", "member")}
	member_role = "test-" + uuid4().hex[:12]
	assert not frappe.db.exists("Concept Value", member_role)
	activity_type, service = make_activity_type(), make_service()
	caseworker = make_user(f"first-wave-{uuid4().hex}@example.invalid", [(ctx.name, "Write")])
	frappe.db.commit()
	try:
		with publication_db(socket, ctx.name, ids, work, member_role) as (
			schema,
			config,
		):
			partial, missing = exercise_cancellation(
				site, ctx.name, ids, schema, config, member_role, caseworker
			)
			result = transfer(
				site,
				ctx.name,
				ids,
				work,
				schema,
				config,
				attempt=3,
				member_role=member_role,
			)
		activated = counts()
		assert all(v == 1 for v in activated.values())
		assert frappe.db.get_value("Case Member", ids["member"], "person_member_role") == member_role
		assert frappe.db.get_value("Case", ids["case"], "case_number") == "SYN-" + ids["case"][:8]
		assert frappe.db.get_value("Person", ids["person"], "person_file_id") == "SYN-" + ids["person"][:8]
		with caseworker_session(caseworker, work) as request:
			numbers = first_user_writes(ctx.name, ids, activity_type, service, request)
			second = later_wave(site, socket, work, ids["case"], request)
		return {
			**result,
			"second_wave": second,
			"initial_pilot_rows": 0,
			"rows_at_activation": activated,
			"discarded_partial_run": partial,
			"rejected_then_discarded_run": missing,
			"new_case_number": numbers[0],
			"new_person_file_id": numbers[1],
			"rehearsal_seconds": round(monotonic() - started, 1),
		}
	finally:
		frappe.db.rollback()
		frappe.set_user("Administrator")
		user = frappe.get_doc("User", caseworker)
		user.enabled = 0
		user.save()
		frappe.db.commit()


def main():
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--site", required=True)
	parser.add_argument("--publisher-socket", type=Path, required=True)
	args = parser.parse_args()
	socket = args.publisher_socket.resolve()
	os.chdir(BENCH / "sites")
	frappe.init(site=args.site)
	frappe.connect()
	try:
		with tempfile.TemporaryDirectory(prefix="ucm-first-wave-") as path:
			print(json.dumps(run(args.site, socket, Path(path))))
	finally:
		frappe.destroy()


if __name__ == "__main__":
	main()
