"""What the publishing side and the Desk ask of a run over HTTP: its status, its report and its metrics, and
the actions of a Migration Admin."""

import json

import frappe

from .contract import digest

RUN = "UCM Migration Run"
BATCH = "UCM Migration Batch"
QUEUE = "ucm_migration"
ROLE = "Migration Admin"
PUBLISHER = "Migration Publisher"
PROPOSED = "Proposed"
AWAITING = "Awaiting publication"


def _operator():
	"""Who runs a migration on this site: the Migration Admin role, which the loader's DocTypes bring with them.
	A System Manager reads the runs and gives the role."""
	frappe.only_for(ROLE)


def _publisher_or_operator(run):
	"""A publisher can upload to and inspect only the exact run an operator assigned."""
	user = frappe.session.user
	if user != "Guest" and frappe.db.get_value(RUN, run, "publishing_user") == user:
		return
	_operator()


def _record(run):
	return frappe.get_doc(RUN, run, for_update=True)


def _result(record):
	return {
		"run": record.name,
		"state": record.state,
		"received": record.received,
		"applied": record.applied or 0,
		"documents": record.document_count,
		"finding": record.finding,
		"reconciled_batches": record.get("reconcile_cursor") or 0,
		"worker_phase": record.get("worker_phase"),
		"last_checkpoint": str(record.worker_seen) if record.get("worker_seen") else None,
	}


@frappe.whitelist(methods=["POST"])
def propose(handoff: dict):
	"""A publishing account proposes the hand-off of a publication it has sealed. It shows in the Desk for a
	Migration Admin to approve; no file is accepted before that."""
	from .operator import propose as proposed

	return proposed(handoff)


@frappe.whitelist(methods=["POST"])
def status(run: str):
	_publisher_or_operator(run)
	return _result(frappe.get_doc(RUN, run))


@frappe.whitelist(methods=["POST"])
def report(run: str):
	_publisher_or_operator(run)
	record = frappe.get_doc(RUN, run)
	from .held_shares import summary

	return {
		**_result(record),
		"held_shares": summary(run),
		"reconciliation": json.loads(record.reconciliation_json) if record.reconciliation_json else None,
		"activated_by": record.approved_by,
		"activated_on": record.activated_on,
	}


@frappe.whitelist(methods=["POST"])
def quality(run: str):
	"""The run's data quality metrics, for the migration side to keep beside its own."""
	from .ucm_migration_loader import run_overview

	_publisher_or_operator(run)
	record = frappe.get_doc(RUN, run)
	if not run_overview.is_publication(json.loads(record.manifest_json)):
		raise ValueError("Metrics are kept for sealed publications")
	if not record.reconciliation_json:
		raise ValueError("The run has no verification report yet")
	document = run_overview.quality(record)
	return {**document, "sha256": digest(document["metrics"])}


@frappe.whitelist(methods=["POST"])
def quality_received(run: str, sha256: str, rows: int, destination: str):
	"""Record that the migration side stored the metrics it was given, so the run shows the hand-over."""
	from .ucm_migration_loader import run_overview

	current = quality(run)
	if sha256 != current["sha256"] or int(rows) != len(current["metrics"]):
		raise ValueError("The run has changed since these metrics were read")
	handover = {
		"rows": int(rows),
		"sha256": sha256,
		"destination": str(destination)[:140],
		"by": frappe.session.user,
		"on": str(frappe.utils.now_datetime()),
	}
	# the run itself has not changed: its metrics keep the time of its last step
	frappe.db.set_value(RUN, run, "quality_json", json.dumps(handover), update_modified=False)
	run_overview.refresh(run)
	return handover


def mark_activated(run):
	frappe.db.set_value(
		RUN,
		run,
		{
			"state": "Activated",
			"approved_by": frappe.session.user,
			"activated_on": frappe.utils.now_datetime(),
			"finding": None,
		},
	)


def _request(run, action, max_batches=None):
	from .publication_jobs import request

	return request(run, action, max_batches)


@frappe.whitelist(methods=["POST"])
def apply(run: str, max_batches: int | None = None):
	return _request(run, "apply", max_batches)


@frappe.whitelist(methods=["POST"])
def pause(run: str):
	return _request(run, "pause")


@frappe.whitelist(methods=["POST"])
def activate(run: str):
	return _request(run, "activate")


@frappe.whitelist(methods=["POST"])
def discard(run: str):
	return _request(run, "discard")
