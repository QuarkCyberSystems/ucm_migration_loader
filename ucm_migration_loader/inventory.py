"""Export the target's UCM fields, physical storage, dependencies and current loader coverage."""

import csv
from pathlib import Path

import frappe

from . import api
from .contract import FIELDS, OTHER_NAMES, PROFILE

COLUMNS = (
	"doctype",
	"module",
	"ownership",
	"parent_tables",
	"field",
	"fieldtype",
	"sql_type",
	"required",
	"link_target",
	"derived_from",
	"coverage",
)


def coverage(dt, field):
	if dt not in (*FIELDS, "Person Other Name"):
		return "Scope review"
	if field in ("name", "context_tech_id", *FIELDS.get(dt, ())):
		return "Published"
	if dt == "Person Other Name" and field in (
		"parent",
		"parenttype",
		"parentfield",
		"idx",
		*OTHER_NAMES,
	):
		return "Published"
	if dt == "Person" and field == "person_other_names":
		return "Published"
	if dt == "Person" and field == "person_all_names":
		return "Derived by UCM"
	if field in ("owner", "modified_by", "creation", "modified"):
		return "Assigned by target; historical policy pending"
	return "Field review"


def export_csv(output_path: str):
	"""Run locally with bench execute. Only schema metadata and coverage labels leave the site."""
	from frappe.model import display_fieldtypes, table_fields
	from ucm.utils.data_model import context_scopes

	api._operator()
	all_types = frappe.get_all(
		"DocType",
		filters={"module": ["in", ["UCM", "UCM Dictionary"]]},
		fields=["name", "module", "issingle", "istable"],
		order_by="name",
	)
	metas = {row.name: frappe.get_meta(row.name) for row in all_types}
	parents = {}
	for dt, meta in metas.items():
		for table in meta.get_table_fields():
			parents.setdefault(table.options, []).append(f"{dt}.{table.fieldname}")
	total = 0
	file = Path(output_path)
	with file.open("x", encoding="utf-8", newline="") as handle:
		writer = csv.DictWriter(handle, fieldnames=COLUMNS)
		writer.writeheader()
		for row in all_types:
			dt, meta = row.name, metas[row.name]
			types = (
				{}
				if row.issingle
				else dict(
					frappe.db.sql(
						"select column_name, column_type from information_schema.columns where table_schema=database() and table_name=%s",
						("tab" + dt,),
					)
				)
			)
			fields = {f.fieldname: f for f in meta.fields if f.fieldtype not in display_fieldtypes}
			ownership = (
				"child"
				if row.istable
				else "singleton"
				if row.issingle
				else context_scopes().get(dt, "global or indirect")
			)
			for field in sorted(set(types) | set(fields)):
				df = fields.get(field)
				link = df.options if df and df.fieldtype in ("Link", "Dynamic Link", *table_fields) else ""
				writer.writerow(
					{
						"doctype": dt,
						"module": row.module,
						"ownership": ownership,
						"parent_tables": " | ".join(sorted(parents.get(dt, []))),
						"field": field,
						"fieldtype": df.fieldtype if df else "Database column",
						"sql_type": types.get(field, ""),
						"required": int(bool(df and df.reqd)),
						"link_target": link,
						"derived_from": df.fetch_from if df else "",
						"coverage": coverage(dt, field),
					}
				)
				total += 1
	return {
		"profile": PROFILE,
		"site": frappe.local.site,
		"doctypes": len(metas),
		"field_rows": total,
		"output": str(file),
	}
