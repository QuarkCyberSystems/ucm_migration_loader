"""Resolve a wave's administrative areas back to country and apply parents before children."""

from itertools import batched

import pymysql

from .countries import _same

LEVELS = range(1, 6)


def parent_field(level):
	return "country_tech_id" if level == 1 else f"admin_level_{level - 1}_tech_id"


def fields(level):
	prefix = f"admin_level_{level}_"
	return (
		"name",
		parent_field(level),
		*(prefix + suffix for suffix in ("code", "name", "latitude", "longitude")),
	)


def area_plan(connection, direct):
	required = {n: set(direct.get(n, ())) for n in LEVELS}
	planned = {}
	with connection.cursor(pymysql.cursors.DictCursor) as cur:
		for n in reversed(LEVELS):
			rows = []
			for keys in batched(sorted(required[n]), 256):
				cur.execute(
					f"select {','.join(fields(n))} from `tabAdmin Level {n}` where name in %s order by name",
					(keys,),
				)
				batch = cur.fetchall()
				if len(batch) != len(keys):
					raise ValueError(f"Publication is missing a referenced Admin Level {n}")
				for r in batch:
					parent = r[parent_field(n)]
					if not parent:
						raise ValueError(f"Published Admin Level {n} lacks its parent")
					if n > 1:
						required[n - 1].add(parent)
				rows.extend(batch)
			planned[n] = rows
	return planned


def apply_areas(plan):
	"""One caller-owned transaction. Matching existing records can be shared between waves."""
	import frappe

	frappe.only_for("Migration Admin")
	result = {}
	for n in LEVELS:
		dt = f"Admin Level {n}"
		created = reused = 0
		for row in plan[n]:
			name = row["name"]
			if set(row) != set(fields(n)):
				raise ValueError(f"Unexpected fields for {dt}")
			stored = frappe.db.get_value(dt, name, fields(n), as_dict=True, for_update=True)
			if stored is None:
				frappe.get_doc({"doctype": dt, **row}).insert(set_name=name)
				created += 1
				stored = frappe.db.get_value(dt, name, fields(n), as_dict=True, for_update=True)
			else:
				reused += 1
			if differing := [f for f in fields(n) if not _same(stored[f], row[f])]:
				raise ValueError(f"{dt} fields differ from publication: {differing}")
		result[dt] = {"created": created, "reused": reused}
	return result
