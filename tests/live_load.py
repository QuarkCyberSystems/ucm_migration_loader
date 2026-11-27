"""Measure an A caseworker reading and saving while the bounded B publication is applied."""

import argparse
import json
import math
import os
import tempfile
import threading
from collections import defaultdict
from pathlib import Path
from time import monotonic
from uuid import uuid4

import frappe
import pymysql
from first_wave import caseworker_session
from rehearse import call, finish, make_wave, new_context, stage, worker
from requests import RequestException
from transport import BENCH, publication_db

from ucm_migration_loader.contract import MAX_DOCUMENTS, plan
from ucm_migration_loader.push import publication, publisher_schema, send


def summary(samples):
	grouped = defaultdict(list)
	for phase, operation, duration, ok in samples:
		grouped[phase, operation].append((duration, ok))
	results = {}
	for (phase, operation), values in grouped.items():
		times = sorted(t for t, ok in values if ok)
		metrics = {"requests": len(values), "errors": len(values) - len(times)}
		for percentile in (50, 95, 99):
			metrics[f"p{percentile}_ms"] = (
				round(times[math.ceil(len(times) * percentile / 100) - 1], 1) if times else None
			)
		results.setdefault(phase, {})[operation] = metrics
	return results


def run(site, socket, work, files, batch_size):
	from ucm.tests.utils import make_user

	a, manifest_a, batches_a = make_wave(site, "synthetic-load-baseline")
	finish(stage(manifest_a, batches_a), a)
	context = new_context("synthetic-load-b").name
	ids = [{k: str(uuid4()) for k in ("case", "person", "other", "member")} for _ in range(files)]
	user = make_user(f"load-{uuid4().hex}@example.invalid", [(manifest_a["context"], "Write")])
	frappe.db.commit()
	samples, durations = [], {}
	stop = threading.Event()
	phase = "baseline"
	try:
		with publication_db(socket, context, ids[0], work, additional_ids=ids[1:]) as (
			schema,
			config,
		):
			with pymysql.connect(**json.loads(config.read_text())) as conn:
				manifest, batches = plan(site, schema, context, publication(conn, context), size=batch_size)
				manifest["schema"] = publisher_schema(conn)
			with caseworker_session(user, work) as request:

				def traffic():
					count = 0
					while not stop.is_set():
						for method, op, kwargs in (
							("GET", "read", {}),
							(
								"PUT",
								"save",
								{"json": {"case_remark": f"Load probe {count}"}},
							),
						):
							measured_phase, started = phase, monotonic()
							try:
								data = request(method, "/api/resource/Case/" + a["case"], **kwargs)["data"]
								ok = data["name"] == a["case"] and (
									op == "read" or data["case_remark"] == kwargs["json"]["case_remark"]
								)
							except RequestException:
								ok = False
							samples.append((measured_phase, op, (monotonic() - started) * 1000, ok))
						count += 1
						stop.wait(0.15)

				thread = threading.Thread(target=traffic)
				thread.start()
				try:
					stop.wait(12)
					started = monotonic()
					phase = "stage"
					run_id = send(call, manifest, batches)["run"]
					durations[phase] = round(monotonic() - started, 2)
					for action, expected in (
						("verify", "Verified"),
						("apply", "Ready"),
						("activate", "Activated"),
					):
						phase, started = action, monotonic()
						call(action, run=run_id)
						worker()
						status = call("status", run=run_id)
						assert status["state"] == expected, status
						durations[phase] = round(monotonic() - started, 2)
				finally:
					stop.set()
					thread.join()
			evidence = call("report", run=run_id)["reconciliation"]
			assert all(item["rows"] == files for item in evidence["entities"].values()), evidence
			measured = summary(samples)
			assert all(
				measured.get(p, {}).get(o, {}).get("requests", 0)
				for p in ("baseline", "apply", "activate")
				for o in ("read", "save")
			), measured
			assert all(ok for _, _, _, ok in samples), measured
			return {
				"site": site,
				"run": run_id,
				"source": schema,
				"case_files": files,
				"parent_documents": files * 3,
				"batches": len(batches),
				"concurrent_caseworkers": 1,
				"timings_seconds": durations,
				"caseworker": measured,
			}
	finally:
		frappe.db.rollback()
		account = frappe.get_doc("User", user)
		account.enabled = 0
		account.save()
		frappe.db.commit()


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--site", required=True)
	parser.add_argument("--publisher-socket", type=Path, required=True)
	parser.add_argument("--case-files", type=int, default=150)
	parser.add_argument("--batch-size", type=int, default=25)
	args = parser.parse_args()
	if not 1 <= args.case_files <= MAX_DOCUMENTS // 3 or not 1 <= args.batch_size <= MAX_DOCUMENTS:
		parser.error("Choose case-file and batch counts within the pilot limit")
	socket = args.publisher_socket.resolve()
	os.chdir(BENCH / "sites")
	frappe.init(site=args.site)
	frappe.connect()
	try:
		with tempfile.TemporaryDirectory(prefix="ucm-live-load-") as work:
			print(json.dumps(run(args.site, socket, Path(work), args.case_files, args.batch_size)))
	finally:
		frappe.destroy()
