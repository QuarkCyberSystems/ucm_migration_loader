"""What the publishing side and the Desk ask of a run over HTTP: its status, its report and its metrics, and
the actions of a Migration Admin."""

import json

import frappe
from frappe import _

from . import operator, publication_jobs, runs
from .constants import RUN
from .contract import digest
from .held_shares import summary
from .ucm_migration_loader import run_overview


@frappe.whitelist(methods=["POST"])
def propose(handoff: dict):
	"""A publishing account proposes the hand-off of a publication it has sealed. It shows in the Desk for a
	Migration Admin to approve; no file is accepted before that."""
	return operator.propose(handoff)


@frappe.whitelist(methods=["POST"])
def status(run: str):
	runs.sender_or_admin(run)
	return runs.result(frappe.get_doc(RUN, run))


@frappe.whitelist(methods=["POST"])
def report(run: str):
	runs.sender_or_admin(run)
	record = frappe.get_doc(RUN, run)
	return {
		**runs.result(record),
		"held_shares": summary(run),
		"reconciliation": json.loads(record.reconciliation_json) if record.reconciliation_json else None,
		"activated_by": record.approved_by,
		"activated_on": record.activated_on,
	}


@frappe.whitelist(methods=["POST"])
def quality(run: str):
	"""The run's data quality metrics, for the migration side to keep beside its own."""
	runs.sender_or_admin(run)
	record = frappe.get_doc(RUN, run)
	if not run_overview.is_publication(json.loads(record.manifest_json)):
		frappe.throw(_("Metrics are kept for sealed publications"))
	if not record.reconciliation_json:
		frappe.throw(_("The run has no verification report yet"))
	document = run_overview.quality(record)
	return {**document, "sha256": digest(document["metrics"])}


@frappe.whitelist(methods=["POST"])
def quality_received(run: str, sha256: str, rows: int, destination: str):
	"""Record that the migration side stored the metrics it was given, so the run shows the hand-over."""
	current = quality(run)
	if sha256 != current["sha256"] or int(rows) != len(current["metrics"]):
		frappe.throw(_("The run has changed since these metrics were read"))
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


@frappe.whitelist(methods=["POST"])
def apply(run: str, max_batches: int | None = None):
	with runs.refusals():
		return publication_jobs.request(run, "apply", max_batches)


@frappe.whitelist(methods=["POST"])
def pause(run: str):
	with runs.refusals():
		return publication_jobs.request(run, "pause")


@frappe.whitelist(methods=["POST"])
def activate(run: str):
	with runs.refusals():
		return publication_jobs.request(run, "activate")


@frappe.whitelist(methods=["POST"])
def discard(run: str):
	with runs.refusals():
		return publication_jobs.request(run, "discard")
