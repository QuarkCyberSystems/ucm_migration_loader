"""Carry a staged publication through the migration queue on the set-based route, on a rehearsal site.

Two commands, run from the bench with the loader's source on PYTHONPATH; each prints its result as JSON:

	rehearse   run the queue's jobs in this process, with a budget of a few seconds a job, and check on the way:
	           a pause for live load, a pause asked by the operator, a failure in the verification, the report,
	           an activation refused after a receipt changed, an activation timed and rolled back, and the
	           discard in several jobs. The run ends discarded.
	worker     ask for the load as Desk and ucm-push do, and follow the bench's own worker until the run is
	           ready or paused. The run stays ready and hidden.

Synthetic data only: the publication is sealed from a site filled by the data generator.
"""

import argparse
import json
import os
import sys
import time
from collections import Counter, deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import frappe

BENCH = Path(__file__).resolve().parents[3]
RUN = "UCM Migration Run"


def connect(site: str) -> None:
	os.chdir(BENCH / "sites")
	frappe.init(site=site)
	frappe.connect()
	# nosemgrep: frappe-semgrep-rules.rules.security.frappe-setuser -- reviewed: a rehearsal run from the bench by its operator; the loader's commands require a System Manager
	frappe.set_user("Administrator")


def stage(path: Path | None, run: str) -> None:
	from ucm_migration_loader import write_routes
	from ucm_migration_loader.publication_receiver import stage as receive

	if path:
		receive(str(path), run)
		frappe.db.commit()
	write_routes.select(run, write_routes.SET_BASED)
	frappe.db.commit()


def record(run: str):
	frappe.db.rollback()  # a new read of the run, past this connection's snapshot
	return frappe.get_doc(RUN, run)


class Queue:
	"""The migration queue, held in this process: a request adds a job, a step runs the oldest one."""

	def __init__(self, run: str):
		self.run, self.jobs, self.phases = run, deque(), Counter()

	def request(self, action: str) -> None:
		from ucm_migration_loader import publication_jobs

		publication_jobs.request(self.run, action)
		frappe.db.commit()

	def step(self) -> None:
		from ucm_migration_loader import publication_jobs

		job = self.jobs.popleft()
		publication_jobs.execute(*job)
		frappe.db.commit()
		self.phases[f"{job[1]}: {record(self.run).worker_phase}"] += 1

	def finish(self) -> None:
		for _ in range(5000):
			if not self.jobs:
				return
			self.step()
		raise AssertionError("the queue did not come to an end")

	def until(self, reached) -> None:
		for _ in range(5000):
			assert self.jobs, "the queue ended before the expected point"
			self.step()
			if reached(record(self.run)):
				return
		raise AssertionError("the expected point was not reached")


def pauses(queue: Queue, run: str) -> dict:
	"""A pause for live load, then a pause asked by the operator."""
	from ucm_migration_loader import migration_load

	# nothing is written while the site's requests are slow
	frappe.conf.ucm_migration_live_p95_ms = 5
	frappe.flags.ucm_live_request_started = time.monotonic() - 0.05
	migration_load.after_request(request=SimpleNamespace(method="GET", path="/api/resource/Case/example"))
	queue.request("apply")
	queue.step()
	assert record(run).state == "Publication paused" and not record(run).applied
	assert "live read" in record(run).finding
	frappe.conf.ucm_migration_live_p95_ms = 0
	# the job already queued finds the run taken by the pause and writes nothing
	queue.request("apply")
	queue.until(lambda current: (current.applied or 0) > 0)
	queue.request("pause")
	applied = record(run).applied
	queue.finish()
	assert record(run).state == "Publication paused" and record(run).applied == applied
	return {"paused_for_live_load": True, "paused_by_operator_after_batch": applied}


def failure(queue: Queue, run: str) -> dict:
	"""A failure in the verification: the run pauses where it was, and the next request continues there."""
	from ucm_migration_loader.ucm_migration_loader import set_based

	queue.request("apply")
	queue.until(lambda current: current.worker_phase == "Verifying" and current.reconcile_cursor)
	cursor = record(run).reconcile_cursor
	with patch.object(set_based, "_sample_batch", side_effect=ValueError("injected rehearsal failure")):
		try:
			queue.step()
		except ValueError:
			pass
		else:
			raise AssertionError("the injected failure was not raised")
	assert record(run).state == "Publication paused" and record(run).reconcile_cursor == cursor
	assert "apply" in record(run).finding
	return {"failed_and_resumed_at_batch": cursor}


def ready(queue: Queue, run: str) -> dict:
	"""The rest of the load, and its report."""
	from ucm_migration_loader.ucm_migration_loader import set_based

	queue.request("apply")
	queue.finish()
	current = record(run)
	report = json.loads(current.reconciliation_json)
	assert current.state == "Publication ready" and not report["findings"], report["findings"]
	assert report["route"] == set_based.ROUTE and current.reconcile_cursor == current.applied
	business = sum(n for doctype, n in report["published_rows"].items() if doctype not in set_based.DEFERRED)
	assert sum(report["rows_compared"].values()) == business
	return {
		"committed_business_batches": current.applied,
		"of_batches": current.received,
		"rows_compared": business,
		"rules_checked": report["rules_checked"],
		"sampled_document_checks": report["sampled_document_checks"],
		"derived_rows_changed": report["derived_rows_changed"],
		"step_seconds": report["seconds"],
	}


def activation(run: str) -> dict:
	"""An activation refused after a receipt changed, then one timed and rolled back."""
	from ucm_migration_loader.ucm_migration_loader import set_based

	receipt = f"{run}-1"
	digest = frappe.db.get_value("UCM Migration Batch", receipt, "digest")
	frappe.db.set_value("UCM Migration Batch", receipt, "digest", "0" * 64, update_modified=False)
	frappe.db.commit()
	try:
		set_based.activate(run, commit=False)
	except ValueError:
		pass
	else:
		raise AssertionError("an activation over a changed receipt was accepted")
	finally:
		frappe.db.set_value("UCM Migration Batch", receipt, "digest", digest, update_modified=False)
		frappe.db.commit()
	assert record(run).state == "Publication ready"
	activated = set_based.activate(run, commit=False)
	assert activated["state"] == "Activated" and record(run).state == "Publication ready"
	return {"changed_receipt_refused": True, "activation_rolled_back_seconds": activated["seconds"]}


def discard(queue: Queue, run: str) -> dict:
	"""The discard, in several jobs."""
	from ucm_migration_loader.ucm_migration_loader import set_based

	started = time.monotonic()
	queue.request("discard")
	queue.finish()
	gone = record(run)
	assert gone.state == "Discarded" and not gone.applied
	left = set_based.stored_counts(gone.context_id, json.loads(gone.manifest_json))
	assert not any(left.values()), left
	return {"discard_seconds": round(time.monotonic() - started, 2)}


def rehearse(run: str, budget: float) -> dict:
	from ucm_migration_loader import publication_jobs
	from ucm_migration_loader.ucm_migration_loader import set_based

	queue = Queue(run)
	with (
		patch.object(publication_jobs, "enqueue", side_effect=lambda *job: queue.jobs.append(job)),
		patch.object(set_based, "BUDGET", budget),
	):
		started = time.monotonic()
		out = pauses(queue, run)
		out.update(failure(queue, run))
		out["ready"] = {**ready(queue, run), "seconds_with_pauses": round(time.monotonic() - started, 2)}
		out.update(activation(run))
		out.update(discard(queue, run))
	return {**out, "jobs": dict(queue.phases)}


def worker(run: str, timeout: float) -> dict:
	from ucm_migration_loader import api

	started, timeline, seen = time.monotonic(), [], None
	api.apply(run)
	frappe.db.commit()
	while time.monotonic() - started < timeout:
		current = record(run)
		at = (current.state, current.worker_phase, current.applied or 0, current.reconcile_cursor or 0)
		if at[:2] != seen:
			seen = at[:2]
			timeline.append([round(time.monotonic() - started, 1), *at])
		if current.state != "Publication applying":
			break
		time.sleep(1)
	current = record(run)
	report = json.loads(current.reconciliation_json or "{}")
	return {
		"state": current.state,
		"finding": current.finding,
		"seconds": round(time.monotonic() - started, 2),
		"findings": report.get("findings"),
		"step_seconds": report.get("seconds"),
		"timeline": timeline,
	}


def main() -> None:
	parser = argparse.ArgumentParser(
		description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
	)
	parser.add_argument("step", choices=["rehearse", "worker"])
	parser.add_argument("--site", required=True)
	parser.add_argument("--run", required=True)
	parser.add_argument("--publication", type=Path, help="Stage this sealed file first")
	parser.add_argument("--budget", type=float, default=5, help="Seconds of batches per rehearsed job")
	parser.add_argument("--timeout", type=float, default=3600, help="Seconds to follow the worker")
	parser.add_argument("--evidence", type=Path, help="Write the result here as well")
	args = parser.parse_args()
	# the site is opened from the bench's sites folder: the paths given are read from here
	publication = args.publication and args.publication.resolve()
	evidence = args.evidence and args.evidence.resolve()
	connect(args.site)
	try:
		stage(publication, args.run)
		result = (
			rehearse(args.run, args.budget) if args.step == "rehearse" else worker(args.run, args.timeout)
		)
	finally:
		frappe.destroy()
	text = json.dumps(result, indent=1, default=str)
	print(text)
	if evidence:
		evidence.write_text(text + "\n")


if __name__ == "__main__":
	sys.exit(main())
