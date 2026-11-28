"""Operator-facing numbers for migration records with immutable publication keys."""

import frappe
from frappe import _
from frappe.model.naming import getseries


def new_run_id():
	return "MIG-" + getseries("UCM Migration Run ID", 4)


def receipt_id(run, sequence, item=None):
	prefix = frappe.db.get_value("UCM Migration Run", run, "display_id")
	if not prefix:
		frappe.throw(_("Migration run is missing its Run ID"))
	batch = f"{prefix}-B{sequence}"
	return batch if item is None else f"{batch}-S{item}"
