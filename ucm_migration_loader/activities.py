"""Build parent-first activity batches from the frozen source and rehearse them through UCM."""

from collections import defaultdict

import pymysql

from .activity_order import DEPENDENCIES, activity_batches
from .stored_rows import business, checked_row, insert_document, row_differences


def documents(source, context, size):
	with (
		source.cursor(pymysql.cursors.SSCursor) as keys,
		source.cursor(pymysql.cursors.DictCursor) as cur,
	):
		keys.execute(
			DEPENDENCIES,
			context,
		)
		# The planner exhausts the streaming cursor into SQLite before yielding its first batch.
		for names in activity_batches(keys, size):
			cur.execute("select * from tabActivity where name in %s", (tuple(names),))
			parents = {r["name"]: business(r) for r in cur.fetchall()}
			cur.execute(
				"select * from `tabActivity Objective` where parent in %s order by parent,idx,name",
				(tuple(names),),
			)
			children = defaultdict(list)
			for row in cur.fetchall():
				items = children[row["parent"]]
				if (
					row["parenttype"] != "Activity"
					or row["parentfield"] != "activity_objectives"
					or row["idx"] != len(items) + 1
				):
					raise ValueError("Invalid published activity objective ownership or order")
				items.append(business(row, child=True))
			yield [(parents[name], children[name]) for name in names]


def insert_activity(
	published,
	objectives,
	crosswalk,
	*,
	source=None,
	collect_findings=False,
	address_from_target=False,
	insert=insert_document,
):
	import frappe

	frappe.only_for("System Manager")
	row = dict(published)
	department = row["protection_department_tech_id"]
	row["protection_department_tech_id"] = crosswalk.get(("Protection Department", department), department)
	name = row["name"]
	doc = frappe.get_doc(
		{
			"doctype": "Activity",
			**row,
			"activity_objectives": [dict(x) for x in objectives],
		}
	)
	insert(doc, set_name=name, set_child_names=False)
	differences = check_activity(
		row,
		source=source,
		address_from_target=address_from_target,
		collect_findings=collect_findings,
	)
	saved = frappe.get_doc("Activity", name)
	if [str(x.name) for x in saved.activity_objectives] != [x["name"] for x in objectives]:
		raise ValueError("Activity objective keys or order differ on target")
	for child in objectives:
		checked_row("Activity Objective", child)
	return differences


def check_activity(row, *, source=None, address_from_target=False, collect_findings=False):
	import frappe

	name = row["name"]
	differences = row_differences("Activity", row)
	# UCM fills geography from the first address on the selected Place. Permit only
	# a previously empty value which independently matches the frozen address.
	if (source is not None or address_from_target) and differences and row["activity_place_tech_id"]:
		geography = {
			"activity_place_country_tech_id": "country_tech_id",
			"activity_place_admin_level_1_tech_id": "place_address_admin_level_1_tech_id",
			"activity_place_admin_level_2_tech_id": "place_address_admin_level_2_tech_id",
			"activity_place_city_tech_id": "place_address_admin_level_3_tech_id",
		}
		if address_from_target:
			rows = frappe.get_all(
				"Place Address",
				filters={
					"parent": row["activity_place_tech_id"],
					"parenttype": "Place",
					"parentfield": "place_addresses",
				},
				fields=list(geography.values()),
				order_by="idx asc, name asc",
				limit=1,
			)
			address = rows[0] if rows else {}
		else:
			with source.cursor(pymysql.cursors.DictCursor) as cur:
				cur.execute(
					"""select * from `tabPlace Address` where parent=%s and parenttype='Place'
                    and parentfield='place_addresses' order by idx,name limit 1""",
					row["activity_place_tech_id"],
				)
				address = cur.fetchone() or {}
		for field, place_field in geography.items():
			if differences.get(field) == "filled_by_target" and address.get(place_field):
				actual = frappe.db.get_value("Activity", name, field)
				if str(actual) == str(address[place_field]):
					differences[field] = "verified_from_published_place"
	if any(v != "verified_from_published_place" for v in differences.values()) and not collect_findings:
		raise ValueError(f"Activity differs from publication: {sorted(differences)}")
	return differences
