"""Provision places reached by activities, including their ordered addresses and protected profile rows."""

import pymysql

from .geography import LEVELS, apply_areas, area_plan
from .stored_rows import business, checked_row


def place_keys(source, ctx):
	with source.cursor() as c:
		c.execute(
			"""select activity_place_tech_id from tabActivity where context_tech_id=%s and activity_place_tech_id is not null
            union select d.activity_detail_place_tech_id from `tabActivity Detail` d join tabActivity a on a.name=d.activity_tech_id
            where a.context_tech_id=%s and d.activity_detail_place_tech_id is not null order by 1""",
			(ctx, ctx),
		)
		return [r[0] for r in c.fetchall()]


def apply_place(source, key, mappings):
	import frappe
	from ucm.permissions import profiles

	frappe.only_for("Migration Admin")
	with source.cursor(pymysql.cursors.DictCursor) as c:
		c.execute("select * from tabPlace where name=%s", key)
		raw = c.fetchone()
		if raw is None:
			raise ValueError("Published activity references a missing place")
		place = business(raw)
		c.execute("select * from `tabPlace Address` where parent=%s order by idx,name", key)
		addresses = [business(r, child=True) for r in c.fetchall()]
		c.execute(
			"select * from `tabSensitive Information` where record_doctype='Place' and record_name=%s order by name",
			key,
		)
		sensitive = [business(r) for r in c.fetchall()]
		for row in sensitive:
			original = row["field_clearance_tech_id"]
			row["field_clearance_tech_id"] = mappings.get(("Field Clearance", original), original)
			c.execute(
				"select field_name from `tabSensitive Field` where record_doctype='Place' and field_clearance_tech_id=%s",
				original,
			)
			source_fields = {r["field_name"] for r in c.fetchall()}
			if (
				not source_fields
				or set(profiles.fields_of("Place", row["field_clearance_tech_id"])) != source_fields
			):
				raise ValueError("Place clearance covers different fields on the target")
	direct = {n: set() for n in LEVELS}
	# The generator writes singleton reference addresses with the SQL default idx=0.
	# Frappe numbers child rows from one when the owning document is inserted.
	normalized = len(addresses) == 1 and addresses[0]["idx"] == 0
	if normalized:
		addresses[0]["idx"] = 1
	for index, row in enumerate(addresses, 1):
		if row["idx"] != index or (row["parenttype"], row["parentfield"]) != (
			"Place",
			"place_addresses",
		):
			raise ValueError("Invalid published place address ownership or order")
		for n in range(1, 4):
			if value := row[f"place_address_admin_level_{n}_tech_id"]:
				direct[n].add(value)
	apply_areas(area_plan(source, direct))
	existing = frappe.db.exists("Place", key)
	if not existing:
		frappe.get_doc(
			{
				"doctype": "Place",
				**place,
				"place_addresses": [dict(row) for row in addresses],
			}
		).insert(set_name=key, set_child_names=False)
	checked_row("Place", place)
	saved = frappe.get_doc("Place", key)
	if [str(r.name) for r in saved.place_addresses] != [r["name"] for r in addresses]:
		raise ValueError("Stored place address list differs")
	for row in addresses:
		checked_row("Place Address", row)
	for row in sensitive:
		if not frappe.db.exists("Sensitive Information", row["name"]):
			frappe.get_doc({"doctype": "Sensitive Information", **row}).insert(set_name=row["name"])
		checked_row("Sensitive Information", row)
	if frappe.db.count("Sensitive Information", {"record_doctype": "Place", "record_name": key}) != len(
		sensitive
	):
		raise ValueError("Additional target place profiles require review")
	return {
		"created": int(not existing),
		"places": 1,
		"addresses": len(addresses),
		"singleton_positions_normalized": int(normalized),
		"sensitive": len(sensitive),
	}
