"""What every part of the loader does to a run on the site: who may act on it, how its record is locked and
answered, and what keeps its delegation closed."""

from contextlib import contextmanager

import frappe
from frappe import _

from .constants import ACTIVATED, ROLE, RUN


def only_admin():
	"""Who runs a migration on this site: the Migration Admin role, which the loader's DocTypes bring with them.
	A System Manager reads the runs and gives the role."""
	frappe.only_for(ROLE)


def sender_or_admin(run):
	"""The publishing account reads and sends the exact run it was approved for, and no other."""
	user = frappe.session.user
	if user != "Guest" and frappe.db.get_value(RUN, run, "publishing_user") == user:
		return
	only_admin()


@contextmanager
def refusals():
	"""Around what a request asks of a run: a refusal reaches the person who asked as a message. The loader
	refuses with ValueError, in sentences without data, because the same code runs in jobs; left alone, a request
	would answer with a server error and no sentence."""
	try:
		yield
	except ValueError as refused:
		# escaped all the same: a message is shown as HTML
		frappe.throw(frappe.utils.escape_html(str(refused)), title=_("Not done"))


def record(run):
	return frappe.get_doc(RUN, run, for_update=True)


def result(record):
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


def lock_run(run):
	"""The run and its delegation's context, both locked: the context first, as every part of the loader does."""
	context = frappe.db.get_value(RUN, run, "context_id")
	ctx = frappe.get_doc("Context", context, for_update=True)
	return ctx, record(run)


def check_closed(ctx):
	if ctx.context_status != "Locked" or ctx.context_discoverability != "Hidden":
		raise ValueError("Application requires a locked, hidden migration context")
	if not frappe.db.get_single_value("System Settings", "disable_document_sharing"):
		raise ValueError("Enable the UCM setting that disables Frappe document sharing")
	if frappe.db.exists("Record Share", {"context_tech_id": ctx.name}):
		raise ValueError("A share exists for the incoming context")


def mark_activated(run):
	frappe.db.set_value(
		RUN,
		run,
		{
			"state": ACTIVATED,
			"approved_by": frappe.session.user,
			"activated_on": frappe.utils.now_datetime(),
			"finding": None,
		},
	)
