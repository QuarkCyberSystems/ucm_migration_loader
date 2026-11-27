"""Materialize the small UCM prerequisites for an isolated generator rehearsal in dependency order."""

import pymysql

from .contract import check_uuid
from .countries import _same
from .dictionary_identity import TERM_COLUMNS, resolve_term
from .scope import CATALOGS

SYSTEM = {"owner", "modified_by", "creation", "modified", "idx", "docstatus"}


def apply_catalogs(source, *, catalogs=CATALOGS, context=None):
	import frappe

	frappe.only_for("Migration Admin")
	if context is not None:
		check_uuid(context)
		with source.cursor() as cur:
			cur.execute("select name from tabContext where name=%s", context)
			if not cur.fetchone():
				raise ValueError("Unknown publishing context")
	counts = {}
	mapping = {}
	if any(dt not in CATALOGS for dt in catalogs):
		raise ValueError("Unknown reference catalog")
	for dt in catalogs:
		table = "`tab" + dt + "`"
		with source.cursor() as cur:
			cur.execute(
				"select column_name from information_schema.columns where table_schema=database() and table_name=%s order by ordinal_position",
				"tab" + dt,
			)
			fields = [f for (f,) in cur.fetchall() if f not in SYSTEM and not f.startswith("_")]
		if not fields or any(not f.isidentifier() for f in fields):
			raise ValueError(f"Unexpected publishing schema for {dt}")
		with source.cursor(pymysql.cursors.DictCursor) as cur:
			where, params = "", ()
			if context is not None and dt == "Context":
				where, params = "where name=%s", (context,)
			elif context is not None and "context_tech_id" in fields:
				where, params = (
					"where context_tech_id is null or context_tech_id=%s",
					(context,),
				)
			cur.execute(
				f"select {','.join(f'`{f}`' for f in fields)} from {table} {where} order by name",
				params,
			)
			rows = cur.fetchall()
		links = {f.fieldname: f.options for f in frappe.get_meta(dt).fields if f.fieldtype == "Link"}
		created = reused = 0
		for row in rows:
			if dt == "Context":
				row.update(
					context_status="Locked",
					context_discoverability="Hidden",
				)
			for f, parent in links.items():
				if parent == "Concept Value" and (key := row.get(f)) and (parent, key) not in mapping:
					with source.cursor() as cur:
						cur.execute(
							f"select {TERM_COLUMNS} from `tabConcept Value` where name=%s",
							key,
						)
						attributes = cur.fetchone()
					if not attributes:
						raise ValueError(f"Missing published dictionary identity for {dt}.{f}")
					resolved, _ = resolve_term(frappe.db._conn, key, attributes)
					if not resolved:
						raise ValueError(f"Unresolved target dictionary identity for {dt}.{f}")
					mapping[parent, key] = resolved
				if (parent, row.get(f)) in mapping:
					row[f] = mapping[parent, row[f]]
			name = row["name"]
			identity = {
				"Protection Department": "department_code",
				"Field Clearance": "clearance_name",
			}.get(dt)
			if identity:
				by_code = frappe.db.get_value(
					dt,
					{identity: row[identity]},
					"name",
					for_update=True,
				)
				if by_code and str(by_code) != name:
					if frappe.db.exists(dt, name):
						raise ValueError(f"{dt} ID and {identity} resolve to different records")
					mapping[dt, name] = row["name"] = name = str(by_code)
			stored = frappe.db.get_value(dt, name, fields, as_dict=True, for_update=True)
			if stored is None:
				if dt == "Context Profile Exemption":
					from .application import check_closed

					check_closed(frappe.get_doc("Context", row["context_tech_id"], for_update=True))
				frappe.get_doc({"doctype": dt, **row}).insert(set_name=name)
				created += 1
				stored = frappe.db.get_value(dt, name, fields, as_dict=True, for_update=True)
			else:
				reused += 1
			if differing := [
				f
				for f in fields
				if not _same(stored[f], row[f])
				and not (
					frappe.get_meta(dt).get_field(f)
					and frappe.get_meta(dt).get_field(f).fieldtype == "Select"
					and row[f] in (None, "")
					and stored[f] in (None, "")
				)
			]:
				raise ValueError(f"{dt} differs on fields {differing}")
		counts[dt] = {"created": created, "reused": reused}
	return {
		"catalogs": counts,
		"crosswalk": [
			{"doctype": dt, "published": key, "target": value} for (dt, key), value in mapping.items()
		],
	}
