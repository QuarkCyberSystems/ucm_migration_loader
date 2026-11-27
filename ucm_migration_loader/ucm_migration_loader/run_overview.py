"""What a migration run's form and list show: its steps with their results and times, the rows on both sides and
the findings of its verification. Everything is read from what the run already keeps: its receipts, its report
and the few times noted here. Sentences are composed on the form, in the user's language."""

import json

from ucm_migration_loader.constants import (
	ACTIVATED,
	APPLYING,
	AWAITING,
	BY_DOCTYPE,
	CLEANING,
	DISCARDED,
	DOCUMENT,
	PAUSED,
	PROPOSED,
	READY,
	REJECTED,
	SET_BASED,
	STAGED,
)

from . import data_quality

# what the first unfinished step is doing, by the run's state
_UNFINISHED = {
	APPLYING: "running",
	PAUSED: "paused",
	STAGED: "waiting",
	CLEANING: "stopped",
	DISCARDED: "stopped",
}
# a run at work whose worker has not been heard of for so many seconds is looked up in the queue
QUIET = 60
RECEIPT_SECONDS = ("write_seconds", "validation_seconds", "attachment_seconds", "verify_seconds")


def route_of(plan: dict) -> str:
	"""The route a run's plan gives: one route for the delegation, or routes chosen per DocType."""
	routes = set(plan.values())
	if not routes:
		return DOCUMENT
	return routes.pop() if len(routes) == 1 else BY_DOCTYPE


def _step(step: str, status: str, seconds=None, facts: dict | None = None) -> dict:
	return {
		"step": step,
		"status": status,
		"seconds": None if seconds is None else round(seconds, 1),
		"facts": facts or {},
	}


def _written(receipts: list[dict]) -> tuple[dict, float]:
	"""What the batch receipts say of the writing: batches, parent rows, seconds, and the rows by route."""
	by_route = {}
	for receipt in receipts:
		by_route[receipt["write_route"]] = by_route.get(receipt["write_route"], 0) + (
			receipt["parent_rows"] or 0
		)
	seconds = sum((receipt[field] or 0) for receipt in receipts for field in RECEIPT_SECONDS)
	facts = {
		"batches": sum(receipt["batches"] for receipt in receipts),
		"parents": int(sum(receipt["parent_rows"] or 0 for receipt in receipts)),
		"by_route": {route: int(rows) for route, rows in by_route.items()},
	}
	return facts, seconds


def _work(run: dict, receipts: list[dict], progress: dict, report: dict, business) -> tuple[list, bool]:
	"""The steps that load the delegation, each as (name, done, seconds, facts), and whether the verification
	found something."""
	set_based, finished = run["route"] == SET_BASED, run["state"] in (READY, ACTIVATED)
	written, write_seconds = _written(receipts)
	all_batches = bool(business and written["batches"] >= business)
	derived = (
		progress.get("derived") if progress.get("derived") is not None else report.get("derived_rows_changed")
	)
	if set_based:
		wrote = finished or derived is not None or all_batches
		compared = (progress.get("compared") or {}).get("seconds") or {}
		verify_seconds = sum((report.get("seconds") or compared).values()) or None
	else:
		wrote = finished or (run["reconciled"] or 0) > 0 or all_batches
		verify_seconds = sum((receipt["reconcile_seconds"] or 0) for receipt in receipts) or None
	work = [("write", wrote, write_seconds or None, written)]
	if set_based:
		derive_seconds = progress.get("derive_seconds") or report.get("derive_seconds")
		work.append(("derive", derived is not None, derive_seconds, {"changed": derived or {}}))
	checked = {
		"rules": report.get("rules_checked"),
		"sampled": report.get("sampled_document_checks"),
		"rows": sum((report.get("rows_compared") or report.get("checked_business_rows") or {}).values()),
		"findings": len(data_quality.findings(report)),
		"reconciled": run["reconciled"] or 0,
		"business": business,
	}
	work.append(("verify", finished, verify_seconds, checked))
	return work, set_based and bool(report.get("findings")) and run["state"] == PAUSED


def _closing(run: dict, report: dict, timeline: dict, handover: dict) -> list[dict]:
	"""The steps after the verification: the hand-over of its metrics, the go-live decision, a discard."""
	state, out = run["state"], []
	gone = state in (CLEANING, DISCARDED)
	if handover:
		# a hand-over counts for the metrics the run holds now: after a later step it is due again
		status = "done" if handover.get("current", True) else "waiting"
		out.append(_step("hand over", status, None, handover))
	elif report and not gone:
		out.append(_step("hand over", "waiting"))
	if state == ACTIVATED:
		facts = {
			"by": run["activated_by"],
			"on": run["activated_on"],
			"released": report.get("released_access_rows") or {},
			"held": report.get("shares_held_at_activation") or 0,
		}
		out.append(_step("activate", "done", timeline.get("activate", {}).get("seconds"), facts))
	elif state == READY:
		out.append(
			_step("activate", "decision", None, {"deferred": report.get("deferred_access_rows") or {}})
		)
	elif not gone:
		out.append(_step("activate", "waiting"))
	if gone:
		seconds = timeline.get("discard", {}).get("seconds")
		out.append(_step("discard", "done" if state == DISCARDED else "running", seconds))
	return out


def steps(
	run: dict, receipts: list[dict], progress: dict, report: dict, timeline: dict, handover: dict
) -> list[dict]:
	"""The steps of a sealed publication's run, in order, each with a status (done, running, paused, waiting,
	failed, decision, stopped), its seconds where they were measured, and the numbers its line shows."""
	state, staged = run["state"], timeline.get("stage", {})
	if state == REJECTED:
		return [_step("stage", "failed", staged.get("seconds"), report.get("preflight"))]
	if state in (AWAITING, PROPOSED):
		# proposed, or approved with its file on the way: nothing else has begun
		arriving = run.get("approval") or {}
		status = "running" if arriving.get("arrived") else "waiting"
		later = [_step(name, "waiting") for name in ("write", "verify", "activate")]
		return [_step("stage", status, None, arriving), *later]
	sealed = {"batches": run["batches"], "rows": run["rows"], "tables": run["tables"]}
	out = [_step("stage", "done", staged.get("seconds"), sealed)]
	business = staged.get("business_batches") or report.get("committed_business_batches")
	work, failed = _work(run, receipts, progress, report, business)
	reached = True  # whether every step before this one is done
	for name, done, seconds, facts in work:
		if name == "verify" and failed:
			status = "failed"
		elif done:
			status = "done"
		elif reached:
			status = _UNFINISHED.get(state, "waiting")
		else:
			status = "stopped" if state in (CLEANING, DISCARDED) else "waiting"
		out.append(_step(name, status, seconds if status != "waiting" else None, facts))
		reached = reached and done
	return out + _closing(run, report, timeline, handover)


def records(published: dict, report: dict, activated: bool) -> list[dict]:
	"""The rows of each table in the publication and in UCM. A table held back until activation shows what was
	released once the delegation is open; before that it has no target count."""
	stored = report.get("stored_rows") or report.get("checked_business_rows") or {}
	released = report.get("released_access_rows") or {}
	held = report.get("shares_held_at_activation") or 0
	rows = []
	for doctype, count in sorted(published.items()):
		row = {"doctype": doctype, "published": count, "stored": None, "held": 0, "deferred": False}
		if doctype in data_quality.DEFERRED:
			row["deferred"] = True
			if activated:
				row["stored"] = released.get(doctype, 0)
				row["held"] = held if doctype == "Record Share" else 0
		elif doctype in stored:
			row["stored"] = stored[doctype]
		rows.append(row)
	return rows


def duration(run_steps: list[dict]) -> float:
	"""The seconds the load worked: every measured step but a discard."""
	return sum(step["seconds"] or 0 for step in run_steps if step["step"] != "discard")


# ---------------------------------------------------------------------------------------------------------------
# on the site


def _json(value) -> dict:
	return json.loads(value) if value else {}


def _receipts(run: str) -> list[dict]:
	import frappe

	sums = ", ".join(f"sum({field}) as {field}" for field in (*RECEIPT_SECONDS, "reconcile_seconds"))
	# nosemgrep: frappe-semgrep-rules.rules.security.frappe-sql-format-injection -- reviewed: the columns are the constants above; the run is a parameter
	return frappe.db.sql(
		f"""select ifnull(write_route, 'Document API') as write_route, count(*) as batches,
			sum(parent_rows) as parent_rows, {sums}
		from `tabUCM Migration Batch` where migration_run = %s
		group by ifnull(write_route, 'Document API')""",
		(run,),
		as_dict=True,
	)


def _delegation(record) -> str:
	"""The delegation as its context names it. Where the site has no context for it yet, as the proposed
	hand-off names it."""
	import frappe

	context = record.context_id
	name, code = frappe.db.get_value("Context", context, ["context_name", "context_code"]) or (None, None)
	if not (name or code):
		proposed = _json(record.get("approval_json")).get("delegation") or {}
		name, code = proposed.get("name"), proposed.get("code")
	if name and code:
		return f"{name} ({code})"
	return name or code or context


def working(run: str) -> bool | None:
	"""Whether the migration queue holds a job of this run, waiting or at work. A job whose process was killed,
	or whose worker went down, leaves the queue; the run keeps the state it had. None where the queue cannot be
	read."""
	import frappe
	from frappe.utils.background_jobs import get_queue

	from ucm_migration_loader import constants

	try:
		queue = get_queue(constants.QUEUE)
		for job_id in (*queue.get_job_ids(), *queue.started_job_registry.get_job_ids()):
			job = queue.fetch_job(job_id)
			kwargs = (job.kwargs if job else None) or {}
			if kwargs.get("site") == frappe.local.site and (kwargs.get("kwargs") or {}).get("run") == run:
				return True
		return False
	except Exception:
		return None


def stalled(record) -> bool:
	"""Whether a run that should be at work has no job: nothing was heard of its worker for a while, and the
	queue holds no job of it."""
	from frappe.utils import get_datetime, now_datetime

	if record.state not in (APPLYING, CLEANING):
		return False
	heard = get_datetime(record.get("worker_seen") or record.modified)
	if (now_datetime() - heard).total_seconds() < QUIET:
		return False
	return working(record.name) is False


def is_publication(manifest: dict) -> bool:
	from ucm_migration_loader.frozen import FORMAT

	return manifest.get("format") == FORMAT


def _parts(record) -> tuple:
	manifest = _json(record.manifest_json)
	report, timeline = _json(record.reconciliation_json), _json(record.get("timeline_json"))
	progress, handover = _json(record.get("reconcile_counts")), _json(record.get("quality_json"))
	run = {
		"approval": _approval(record) if record.state in (AWAITING, PROPOSED) else None,
		"state": record.state,
		"route": route_of(_json(record.write_plan_json)),
		"batches": record.received,
		"rows": record.document_count,
		"tables": len(manifest.get("schema") or {}),
		"reconciled": record.get("reconcile_cursor"),
		"activated_by": record.approved_by,
		"activated_on": str(record.activated_on) if record.activated_on else None,
	}
	receipts = _receipts(record.name)
	run_steps = steps(run, receipts, progress, report, timeline, handover)
	if handover.get("sha256") and report and not _handed_over(record, report, run, run_steps, handover):
		handover["current"] = False
		run_steps = steps(run, receipts, progress, report, timeline, handover)
	return manifest, report, run, run_steps


def _handed_over(record, report: dict, run: dict, run_steps: list[dict], handover: dict) -> bool:
	"""Whether the metrics the migration side acknowledged are the ones the run holds now."""
	from ucm_migration_loader.contract import digest

	return digest(_metrics(record, report, run, run_steps)["metrics"]) == handover["sha256"]


def _approval(record) -> dict:
	"""Who approved a publication that waits for its file, for whom, and how much of the file has arrived."""
	from ucm_migration_loader.publication_transport import folder, progress

	approval = _json(record.get("approval_json"))
	out = {key: approval.get(key) for key in ("by", "on", "publisher", "proposed_by", "proposed_on")}
	out["bytes"] = approval.get("size")
	out["arrived"] = 0
	if (
		record.state == AWAITING
		and (folder(record.name) / "approval.json").is_file()
		and approval.get("size")
	):
		out["arrived"] = progress(folder(record.name), {**approval, "run": record.name, "site": ""})["offset"]
	return out


def _people(record) -> dict:
	"""Who proposed, approved and staged a run, and when. A run staged without an approval was made by its
	staging, so its record's author and date are the staging's."""
	approval, stage = _json(record.get("approval_json")), _json(record.get("timeline_json")).get("stage", {})
	out = {
		"proposed_on": approval.get("proposed_on"),
		"proposed_by": approval.get("proposed_by"),
		"approved_on": approval.get("on"),
		"approved_by": approval.get("by"),
		"staged_on": stage.get("at") if stage.get("by") else None,
		"staged_by": stage.get("by"),
	}
	if not approval and record.state not in (AWAITING, PROPOSED):
		out["staged_on"] = out["staged_on"] or str(record.creation)
		out["staged_by"] = out["staged_by"] or record.owner
	return out


def build(record) -> dict:
	"""Everything the form's overview shows for one run."""
	import frappe

	manifest = _json(record.manifest_json)
	if not is_publication(manifest):
		return {"publication": False}
	manifest, report, run, run_steps = _parts(record)
	url = frappe.conf.get("ucm_migration_quality_url")
	no_job = stalled(record)
	for step in run_steps:
		if no_job and step["status"] == "running":
			step["status"] = "no job"
	return {
		"publication": True,
		"state": record.state,
		"stalled": no_job,
		"batches_committed": record.applied or 0,
		"finding": record.finding,
		"delegation": _delegation(record),
		"steps": run_steps,
		"records": records(manifest.get("counts") or {}, report, record.state == ACTIVATED),
		"findings": data_quality.findings(report),
		"facts": {
			"route": None if record.state in (REJECTED, AWAITING, PROPOSED) else run["route"],
			"snapshot": record.snapshot_id,
			**_people(record),
			"publishing_user": record.publishing_user,
			"versions": manifest.get("application_versions") or {},
		},
		"quality_url": url.format(run=record.name, context=record.context_id) if url else None,
	}


def refresh(run: str) -> None:
	"""Keep the columns of the run list in step with the run: its delegation, its route and the time it worked."""
	import frappe

	from ucm_migration_loader import constants

	record = frappe.get_doc(constants.RUN, run)
	manifest = _json(record.manifest_json)
	# a refused publication was given no route; a run without a measured step shows no time
	route = (
		None if record.state in (REJECTED, AWAITING, PROPOSED) else route_of(_json(record.write_plan_json))
	)
	values = {"delegation": _delegation(record)[:140], "write_route": route}
	if is_publication(manifest):
		values["duration"] = round(duration(_parts(record)[3])) or None
	frappe.db.set_value(constants.RUN, run, values, update_modified=False)


def note(run: str, step: str, seconds: float | None = None, **facts) -> None:
	"""Keep on the run how long a step outside the batches took (staging, activation, discard). The seconds of a
	step done over several jobs add up."""
	import frappe
	from frappe.utils import now_datetime

	from ucm_migration_loader import constants

	timeline = _json(frappe.db.get_value(constants.RUN, run, "timeline_json"))
	entry = timeline.setdefault(step, {})
	if seconds is not None:
		entry["seconds"] = round(entry.get("seconds", 0) + seconds, 2)
	entry.update(facts, at=str(now_datetime()))
	frappe.db.set_value(constants.RUN, run, "timeline_json", json.dumps(timeline), update_modified=False)


def staged(run: str, seconds: float, business_batches: int) -> None:
	"""A run has just been staged: keep how long that took and where its business batches end."""
	import frappe

	note(run, "stage", seconds, business_batches=business_batches, by=frappe.session.user)
	refresh(run)


def quality(record) -> dict:
	"""The metric rows of a run for the migration side's data quality database."""
	_manifest, report, run, run_steps = _parts(record)
	return _metrics(record, report, run, run_steps)


def _metrics(record, report: dict, run: dict, run_steps: list[dict]) -> dict:
	import frappe

	code = frappe.db.get_value("Context", record.context_id, "context_code")
	identity = {
		"run": record.name,
		"site": frappe.local.site,
		"context": record.context_id,
		"context_code": code,
		"snapshot": record.snapshot_id,
		"route": run["route"],
		"state": record.state,
		# the time of the run's last change, so that the same report always gives the same rows
		"generated_on": str(record.modified),
	}
	return data_quality.metrics(identity, report, run_steps)
