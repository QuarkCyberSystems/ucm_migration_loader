"""What keeps an incoming delegation closed while its run is at work: the lock on its context, and the guards
on the context and on shares."""

import frappe
from frappe import _

from . import constants, runs

# the DocTypes whose shares are held back while their delegation is loading
SHARED = ("Person", "Case", "Case Member", "Person Consent", "Activity")


def guard_context(doc, method=None):
	if doc.is_new() or frappe.flags.ucm_publishing_context == doc.name:
		return
	previous = doc.get_doc_before_save()
	if previous and (
		previous.context_status == "Locked"
		and previous.context_discoverability == "Hidden"
		and (doc.context_status != "Locked" or doc.context_discoverability != "Hidden")
	):
		refuse_if_applying(doc.name)


def guard_share(doc, method=None):
	if doc.share_doctype in SHARED and doc.share_name:
		owner = frappe.db.get_value(doc.share_doctype, doc.share_name, "context_tech_id")
		if (
			owner
			and frappe.flags.ucm_publishing_context != owner
			and frappe.db.get_value("Context", owner, "context_status") == "Locked"
		):
			refuse_if_applying(owner)


def refuse_if_applying(context):
	frappe.db.get_value("Context", context, "name", for_update=True)
	if frappe.db.get_value(
		constants.RUN,
		{"context_id": context, "state": ["in", constants.HIDDEN]},
		"name",
		for_update=True,
	):
		frappe.throw(_("This migration is applying hidden batches. Activate or discard its run first."))
