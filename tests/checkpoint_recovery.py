"""Exercise a disposable staged 60-batch Person/Person Note fixture on a rehearsal site."""

import argparse
import json
import time
from collections import deque
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from ucm_migration_loader import migration_load, write_routes
from ucm_migration_loader import publication_jobs as jobs
from ucm_migration_loader import publication_receiver as receiver
from ucm_migration_loader import publication_worker as worker


def exercise(site, run):
	frappe.init(site=site)
	frappe.connect()
	try:
		assert frappe.get_doc("UCM Migration Run", run).state == jobs.STAGED
		assert frappe.get_doc("UCM Migration Run", run).received == 60
		return run_checks(run)
	finally:
		frappe.destroy()


def run_checks(run):
	q = deque()

	def record():
		return frappe.get_doc("UCM Migration Run", run)

	def request(action):
		jobs.request(run, action)
		frappe.db.commit()

	def step():
		jobs.execute(*q.popleft())
		frappe.db.commit()

	def finish():
		for _ in range(200):
			if not q:
				return
			step()
		raise AssertionError("unexpected worker loop")

	with patch.object(jobs, "enqueue", side_effect=lambda *args: q.append(args)):
		write_routes.configure(run, {"Person": write_routes.SQL, "Person Note": write_routes.DOCUMENT})
		frappe.conf.ucm_migration_live_p95_ms = 5
		frappe.flags.ucm_live_request_started = time.monotonic() - 0.05
		migration_load.after_request(request=SimpleNamespace(method="GET", path="/api/resource/Case/example"))
		assert "live read" in migration_load.pressure()
		request("apply")
		step()
		assert record().state == jobs.PAUSED and not record().applied
		frappe.conf.ucm_migration_live_p95_ms = 0
		request("apply")
		for _ in range(200):
			step()
			if record().reconcile_cursor:
				break
		cursor = record().reconcile_cursor
		assert 0 < cursor < 60
		with patch.object(
			worker,
			"reconcile_item",
			side_effect=ValueError("injected rehearsal failure"),
		):
			try:
				step()
			except ValueError:
				pass
			else:
				raise AssertionError("expected failure")
		assert record().state == jobs.PAUSED and record().reconcile_cursor == cursor
		request("apply")
		finish()
		assert record().state == jobs.READY and record().reconcile_cursor == 60
		with receiver.indexed_publication(run, record()) as (db, *_):
			item = json.loads(db.execute("select payload from batches where sequence=1").fetchone()[0])[0]
		key = item["row"]["name"]
		assert item["doctype"] == "Person"
		original = frappe.db.get_value("Person", key, "person_name")
		frappe.db.set_value(
			"Person",
			key,
			"person_name",
			"Changed in controlled rehearsal",
			update_modified=False,
		)
		frappe.db.commit()
		request("activate")
		try:
			step()
		except ValueError:
			pass
		else:
			raise AssertionError("target drift accepted")
		assert record().state == jobs.READY
		frappe.db.set_value("Person", key, "person_name", original, update_modified=False)
		frappe.db.commit()
		request("activate")
		finish()
		assert record().state == "Activated" and record().worker_phase == "Activated"
		report = json.loads(record().reconciliation_json)
		return {
			"committed_batches": record().applied,
			"recovered_checkpoint": cursor,
			"unchanged_batches": report["unchanged_batches_verified_under_lock"],
			"load_pause": True,
			"target_drift_rejected": True,
		}


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--site", required=True)
	parser.add_argument("--run", required=True)
	args = parser.parse_args()
	print(json.dumps(exercise(args.site, args.run)))
