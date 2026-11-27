import json

import frappe
from frappe import _
from frappe.model.document import Document

from ucm_migration_loader.display_ids import new_run_id


class UCMMigrationRun(Document):
	def before_insert(self):
		self.display_id = new_run_id()


@frappe.whitelist()
def overview(run: str):
	"""The steps, the rows on both sides and the findings that the run's form shows, for who may read the run."""
	from ucm_migration_loader.ucm_migration_loader import run_overview

	record = frappe.get_doc("UCM Migration Run", run)
	record.check_permission("read")
	return run_overview.build(record)


def _handoff(text: str) -> dict:
	"""A hand-off as the browser read it from the file a person chose."""
	try:
		handoff = json.loads(text)
	except ValueError:
		handoff = None
	if not isinstance(handoff, dict):
		frappe.throw(_("This file is not a hand-off: choose the handoff.json of the sealed publication"))
	return handoff


@frappe.whitelist(methods=["POST"])
def inspect_handoff(handoff: str):
	"""What a hand-off says, for the Migration Admin who is about to approve it. Nothing is changed."""
	from ucm_migration_loader import operator

	return operator.inspect_handoff(_handoff(handoff))


@frappe.whitelist(methods=["POST"])
def approve_handoff(
	handoff: str, publishing_user: str, context_code: str | None = None, context_name: str | None = None
):
	"""Approve a hand-off for the account that will send its file. The publication becomes a run that waits
	for the file, and is staged when all of it has arrived."""
	from ucm_migration_loader import operator

	return operator.approve(_handoff(handoff), publishing_user, context_code, context_name)


@frappe.whitelist(methods=["POST"])
def inspect_proposed(run: str):
	"""What a proposed hand-off says, for the Migration Admin who is about to approve it."""
	from ucm_migration_loader import operator

	return operator.inspect_proposed(run)


@frappe.whitelist(methods=["POST"])
def approve_proposed(
	run: str, check_code: str, context_code: str | None = None, context_name: str | None = None
):
	"""Approve a hand-off its publishing account proposed, with the check code the publishing operator gave."""
	from ucm_migration_loader import operator

	return operator.approve_proposed(run, check_code, context_code, context_name)


@frappe.whitelist(methods=["POST"])
def stage_now(run: str):
	"""Stage the arrived file of a run from the Desk: after a staging that failed, or a refusal whose cause
	was corrected."""
	from ucm_migration_loader import operator

	try:
		return operator.stage_arrived(run)
	except ValueError as failed:
		# the loader's own refusals are sentences without data; escaped all the same, as a message is shown as HTML
		frappe.throw(_("Staging failed: {0}").format(frappe.utils.escape_html(str(failed))))
