"""Prove simple target batches unchanged under release locks; otherwise validate each row."""

import hashlib
import json

import frappe

from .frozen import CHILDREN


def fingerprint(items, context):
	dt = items[0]["doctype"]
	if dt not in ("Person", "Person Note") or any(
		item["doctype"] != dt or item.get("files") or any(item["children"].values()) for item in items
	):
		return None
	keys = [item["row"]["name"] for item in items]
	meta = frappe.get_meta(dt)
	children = {field.options for field in meta.get_table_fields()}
	if children != set(CHILDREN.get(dt, ())):
		return None
	rows = frappe.db.get_values(
		dt, {"name": ["in", keys]}, "*", as_dict=True, order_by="name", for_update=True
	)
	if len(rows) != len(keys) or any(str(row.context_tech_id) != context for row in rows):
		return None
	# Populated dictionary, dynamic or other scoped links require current policy checks.
	allowed_links = {"context_tech_id"}
	if dt == "Person Note":
		allowed_links.add("person_tech_id")
	external = [
		f.fieldname
		for f in meta.fields
		if f.fieldtype in ("Link", "Dynamic Link") and f.fieldname not in allowed_links
	]
	if any(row.get(field) for row in rows for field in external):
		return None
	for child in children:
		if frappe.db.get_values(child, {"parent": ["in", keys], "parenttype": dt}, "name", for_update=True):
			return None
	if dt == "Person":
		if frappe.db.get_values(
			"File",
			{"attached_to_doctype": dt, "attached_to_name": ["in", keys]},
			"name",
			for_update=True,
		):
			return None
	else:
		parents = {row.person_tech_id for row in rows}
		locked = frappe.db.get_values(
			"Person",
			{"name": ["in", list(parents)]},
			["name", "context_tech_id"],
			for_update=True,
		)
		if len(locked) != len(parents) or any(str(owner) != context for _, owner in locked):
			return None
	payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
	return hashlib.sha256(payload.encode()).hexdigest()
