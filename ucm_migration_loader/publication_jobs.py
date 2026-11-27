"""Run Desk and CLI commands for sealed publications on the migration queue."""

import json
from time import monotonic

import frappe
from frappe.utils import now_datetime

from . import api, write_routes
from .application import check_closed, lock_run
from .frozen import FORMAT

STAGED = "Publication staged"
APPLYING = "Publication applying"
PAUSED = "Publication paused"
READY = "Publication ready"
CLEANING = "Publication cleaning"
BATCHES_PER_JOB = 25
# A job starts no batch after this many seconds: a batch of a thousand records with pictures takes
# close to a minute, and 25 of them do not fit the queue's timeout.
JOB_SECONDS = 120


def set_based(run):
	"""Whether the run's plan is the set-based route, whose steps the queue runs in place of the batches."""
	plan = frappe.db.get_value(api.RUN, run, "write_plan_json")
	return write_routes.set_based(json.loads(plan or "{}"))


def is_publication(run):
	manifest = frappe.db.get_value(api.RUN, run, "manifest_json")
	return bool(manifest and json.loads(manifest).get("format") == FORMAT)


def request(run, action, max_batches=None):
	api._operator()
	ctx, record = lock_run(run)
	if action == "discard" and record.state == "Discarded":
		from .publication_receiver import remove_after_discard

		remove_after_discard(record)
		return api._result(record)
	if action == "activate" and record.state == "Activated":
		return api._result(record)
	if max_batches is not None and max_batches < 1:
		raise ValueError("Select a positive batch limit")
	allowed = {
		"apply": (STAGED, APPLYING, PAUSED),
		"pause": (APPLYING, PAUSED),
		"activate": (READY,),
		"discard": (STAGED, APPLYING, PAUSED, READY, CLEANING),
	}
	if record.state not in allowed[action]:
		raise ValueError(f"Cannot {action} a run in {record.state}")
	check_closed(ctx, pilot=False)
	next_state = {
		"apply": APPLYING,
		"pause": PAUSED,
		"activate": READY,
		"discard": CLEANING,
	}[action]
	ticket = (record.dispatch_id or 0) + 1
	frappe.db.set_value(api.RUN, run, {"state": next_state, "dispatch_id": ticket, "finding": None})
	if action != "pause":
		limit = max_batches or record.received + 1
		cursor = record.applied or 0
		stop_at = max(0, cursor - limit) if action == "discard" else cursor + limit
		enqueue(run, action, ticket, limit, stop_at)
	return {
		"run": run,
		"state": next_state,
		"requested": action,
		"applied": record.applied or 0,
	}


def enqueue(run, action, ticket, max_batches, stop_at):
	timeout = None
	if set_based(run):
		# a set-based step works on a whole delegation at once, and is given the time for it
		from .ucm_migration_loader.set_based import JOB_TIMEOUT as timeout

	frappe.enqueue(
		"ucm_migration_loader.publication_jobs.execute",
		queue=api.QUEUE,
		timeout=timeout,
		run=run,
		action=action,
		ticket=ticket,
		max_batches=max_batches,
		stop_at=stop_at,
		enqueue_after_commit=True,
	)


def continue_set_based(run, action, ticket, max_batches, stop_at):
	"""Take one step of a set-based run, and queue the next job while steps remain."""
	from .ucm_migration_loader import set_based as route

	if route.job(run, action, ticket, stop_at):
		_, current = lock_run(run)
		if current.dispatch_id == ticket:
			enqueue(run, action, ticket, max_batches, stop_at)


def _keep_summary(run, action, seconds):
	"""After a job: the time an activation or a discard took, and the columns of the run list."""
	from .ucm_migration_loader import run_overview

	try:
		if seconds is not None and action in ("activate", "discard"):
			run_overview.note(run, action, seconds)
		run_overview.refresh(run)
		# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: the end of a queued job, whose work is committed or rolled back by now; kept also when the job failed
		frappe.db.commit()
	except Exception:
		# what the list shows must never fail a load
		frappe.db.rollback()
		frappe.log_error(title="UCM migration run summary")


def execute(run, action, ticket, max_batches, stop_at=None):
	api._operator()
	started, worked = monotonic(), False
	try:
		worked = _execute(run, action, ticket, max_batches, stop_at)
	finally:
		_keep_summary(run, action, monotonic() - started if worked else None)


def _execute(run, action, ticket, max_batches, stop_at):
	"""Run one job. Returns whether it held the run and did its work."""
	from . import publication_cleanup, publication_worker

	try:
		if action == "activate":
			from .held_shares import lock_activation

			lock_activation()
		_, record = lock_run(run)
		if record.dispatch_id != ticket:
			return False
		if set_based(run):
			continue_set_based(run, action, ticket, max_batches, stop_at)
			return True
		if action == "activate":
			publication_worker.activate(run)
			return True
		cursor = record.applied or 0
		if stop_at is None:  # Jobs already in Redis before cursor bounds were introduced.
			stop_at = max(0, cursor - max_batches) if action == "discard" else cursor + max_batches
		limit = min(max_batches, BATCHES_PER_JOB)
		deadline = monotonic() + JOB_SECONDS
		if action == "discard":
			if record.state != CLEANING:
				return False
			result = publication_cleanup.discard(
				run, limit, ticket=ticket, stop_at=stop_at, deadline=deadline
			)
			more = result["state"] == CLEANING and result["applied"] > stop_at
		elif action == "apply":
			if record.state != APPLYING:
				return False
			result = publication_worker.apply(run, limit, ticket=ticket, stop_at=stop_at, deadline=deadline)
			more = result["state"] == APPLYING and result["applied"] < stop_at
		else:
			raise ValueError("Unknown queued migration action")
		if more:
			_, current = lock_run(run)
			if current.dispatch_id == ticket:
				enqueue(run, action, ticket, max_batches, stop_at)
		return True
	except Exception as exc:
		frappe.db.rollback()
		_, record = lock_run(run)
		if record.dispatch_id == ticket:
			where = f"after committed batch {record.applied or 0}"
			if location := getattr(exc, "migration_location", None):
				sequence, position, dt = location
				where = f"batch {sequence}, item {position} ({dt})"
			update = {
				"finding": f"{action} {where}: {type(exc).__name__}",
				"worker_phase": f"{action} failed",
				"worker_seen": now_datetime(),
			}
			if action == "apply" and record.state == APPLYING:
				update["state"] = PAUSED
			frappe.db.set_value(api.RUN, run, update)
			frappe.db.commit()
		raise
