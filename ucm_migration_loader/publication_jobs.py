"""Run Desk and CLI commands for sealed publications on the migration queue."""

import json
from time import monotonic

import frappe
from frappe.utils import now_datetime

from . import constants, runs, write_routes
from .constants import APPLYING, CLEANING, PAUSED, READY, STAGED
from .runs import check_closed, lock_run

BATCHES_PER_JOB = 25
# A job starts no batch after this many seconds: a batch of a thousand records with pictures takes
# close to a minute, and 25 of them do not fit the queue's timeout.
JOB_SECONDS = 120


def set_based(run):
	"""Whether the run's plan is the set-based route, whose steps the queue runs in place of the batches."""
	plan = frappe.db.get_value(constants.RUN, run, "write_plan_json")
	return write_routes.set_based(json.loads(plan or "{}"))


def request(run, action, max_batches=None):
	runs.only_admin()
	# a proposal may be for a delegation the site has no context for yet: it is declined before any context
	# is looked for
	waiting = frappe.db.get_value(constants.RUN, run, "state", for_update=True)
	if action == "discard" and waiting in (constants.AWAITING, constants.PROPOSED):
		return _withdraw(run)
	if action == "discard" and waiting == constants.REJECTED:
		return _clear_refused(run)
	ctx, record = lock_run(run)
	if action == "discard" and record.state == constants.DISCARDED:
		from .publication_receiver import remove_after_discard

		remove_after_discard(record)
		return runs.result(record)
	if action == "activate" and record.state == constants.ACTIVATED:
		return runs.result(record)
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
	check_closed(ctx)
	next_state = {
		"apply": APPLYING,
		"pause": PAUSED,
		"activate": READY,
		"discard": CLEANING,
	}[action]
	ticket = (record.dispatch_id or 0) + 1
	frappe.db.set_value(constants.RUN, run, {"state": next_state, "dispatch_id": ticket, "finding": None})
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


def _clear_refused(run):
	"""Remove what a refused run keeps on the site: the file that arrived, and its approval. The run stays as
	the record of the refusal and of what differed, and says who removed its file; that file cannot be staged
	afterwards, and a corrected publication is sealed and approved anew."""
	from .publication_receiver import remove_arrived, remove_staged_file

	approval = json.loads(frappe.db.get_value(constants.RUN, run, "approval_json") or "{}")
	if not approval.get("removed_on"):
		approval.update(removed_by=frappe.session.user, removed_on=str(now_datetime()))
		frappe.db.set_value(constants.RUN, run, "approval_json", json.dumps(approval))
	# under its staged name too, where a staging died after the rename and the next one refused the file
	remove_staged_file(run)
	remove_arrived(run)
	return {"run": run, "state": constants.REJECTED, "requested": "discard", "applied": 0}


def _withdraw(run):
	"""Decline a proposed hand-off, or take back the approval of a publication whose file was not staged: what
	arrived of it is removed, and the delegation's context is free for another publication."""
	from .publication_receiver import remove_arrived
	from .ucm_migration_loader import run_overview

	frappe.db.set_value(constants.RUN, run, {"state": constants.DISCARDED, "finding": None})
	run_overview.refresh(run)
	remove_arrived(run)
	return {"run": run, "state": constants.DISCARDED, "requested": "discard", "applied": 0}


def enqueue(run, action, ticket, max_batches, stop_at):
	timeout = None
	if set_based(run) or action == "discard":
		# a set-based step works on a whole delegation at once, and is given the time for it; the rows of every
		# route are discarded in sets
		from .ucm_migration_loader.set_based import JOB_TIMEOUT as timeout

	frappe.enqueue(
		"ucm_migration_loader.publication_jobs.execute",
		queue=constants.QUEUE,
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


def _roll_back() -> None:
	"""Undo what a failed job left open. A job whose database connection was lost takes a new one, so that the
	run can say what happened to it."""
	try:
		frappe.db.rollback()
	except Exception:
		frappe.db.close()
		frappe.db.connect()


def execute(run, action, ticket, max_batches, stop_at=None):
	runs.only_admin()
	started, worked = monotonic(), False
	try:
		worked = _execute(run, action, ticket, max_batches, stop_at)
	finally:
		_keep_summary(run, action, monotonic() - started if worked else None)


def _execute(run, action, ticket, max_batches, stop_at):
	"""Run one job. Returns whether it held the run and did its work."""
	from . import publication_worker

	try:
		if action == "activate":
			from .held_shares import lock_activation

			lock_activation()
		_, record = lock_run(run)
		if record.dispatch_id != ticket:
			return False
		if set_based(run) or action == "discard":
			continue_set_based(run, action, ticket, max_batches, stop_at)
			return True
		if action == "activate":
			publication_worker.activate(run)
			return True
		cursor = record.applied or 0
		if stop_at is None:  # Jobs already in Redis before cursor bounds were introduced.
			stop_at = cursor + max_batches
		limit = min(max_batches, BATCHES_PER_JOB)
		deadline = monotonic() + JOB_SECONDS
		if action == "apply":
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
		_roll_back()
		_, record = lock_run(run)
		if record.dispatch_id == ticket:
			where = f"after committed batch {record.applied or 0}"
			if location := getattr(exc, "migration_location", None):
				sequence, position, dt = location
				where = f"batch {sequence}, item {position} ({dt})"
			# what the site lacks is said in a sentence without data; any other failure gives its kind only
			said = str(exc) if isinstance(exc, runs.Refusal) else type(exc).__name__
			update = {
				"finding": f"{action} {where}: {said}",
				"worker_phase": f"{action} failed",
				"worker_seen": now_datetime(),
			}
			if action == "apply" and record.state == APPLYING:
				update["state"] = PAUSED
			frappe.db.set_value(constants.RUN, run, update)
			frappe.db.commit()
		raise
