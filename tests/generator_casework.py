"""Rehearse interdependent generated casework through UCM on preprovisioned hidden contexts."""

import argparse
import json
import os
from collections import Counter
from pathlib import Path

import frappe
import pymysql
from generator_actors import ensure_actor
from generator_collisions import read_site
from generator_snapshot import BENCH, ownership, site_connection

from ucm_migration_loader.activities import documents, insert_activity
from ucm_migration_loader.history import insert_history
from ucm_migration_loader.parent_publication import case_documents
from ucm_migration_loader.persons import insert_person, map_terms, person_records
from ucm_migration_loader.shared_catalogs import apply_catalogs
from ucm_migration_loader.stored_rows import business, row_differences


def projection(source, context, dt):
	where, n = ownership(dt)
	if dt == "Sensitive Information":
		# This rollback-only casework probe creates only the person and activity owners.
		where = "where r.record_doctype != 'Organization' and (" + where.removeprefix("where ") + ")"
	with source.cursor(pymysql.cursors.DictCursor) as cur:
		cur.execute(f"select r.* from `tab{dt}` r {where} order by r.name", (context,) * n)
		return [business(row) for row in cur.fetchall()]


def exercise(source, target):
	from ucm.ucm.doctype.activity_detail.latest_state import bulk_load

	if source == target or not target.startswith("ucm-full-wave-") or not target.endswith(".localhost"):
		raise ValueError("Select an isolated full-wave rehearsal target")
	with read_site(source) as con:
		with con.cursor() as c:
			c.execute("select name,context_code from tabContext order by context_code")
			contexts = c.fetchall()
		private = (BENCH / "sites" / source / "private/files").resolve(strict=True)
		frappe.init(site=target)
		frappe.connect()
		try:
			crosswalk = {
				(r["doctype"], r["published"]): r["target"] for r in apply_catalogs(con)["crosswalk"]
			}
			results = {}
			with site_connection() as dest:
				cache = {}
				for ctx, code in contexts:
					with con.cursor() as c:
						c.execute(
							"""select count(*) from tabActivity a join `tabActivity Type` t on t.name=a.activity_type_tech_id
                            where a.context_tech_id=%s and t.activity_type_enabled_place=1 and t.activity_type_place_required=1 and a.activity_place_tech_id is null""",
							ctx,
						)
						if missing := c.fetchone()[0]:
							results[code] = {"blocked_missing_places": missing}
							continue
					counts = Counter()
					expected = []
					findings = Counter()
					for record in person_records(con, ctx):
						counts.update(insert_person(con, dest, record, private, cache))
					for case in case_documents(con, ctx):
						frappe.get_doc(case).insert(set_name=case["name"], set_child_names=False)
						counts["Case"] += 1
					for batch in documents(con, ctx, 25):
						for act, children in batch:
							diffs = insert_activity(
								act,
								children,
								crosswalk,
								source=con,
								collect_findings=True,
							)
							findings.update(f"Activity.{f} {v}" for f, v in diffs.items())
							counts["Activity"] += 1
					with bulk_load():
						for dt in (
							"Case Member",
							"Activity Case",
							"Person Relationship",
							"Person Consent",
							"Person Note",
							"Activity Note",
							"Activity Detail",
							"Activity Detail Note",
							"Sensitive Information",
							"Record Tag",
							"ToDo",
						):
							for row in projection(con, ctx, dt):
								map_terms(con, dest, row, dt, cache)
								for field in frappe.get_meta(dt).fields:
									if (
										field.fieldtype == "Link"
										and (field.options, row.get(field.fieldname)) in crosswalk
									):
										row[field.fieldname] = crosswalk[field.options, row[field.fieldname]]
								if dt == "ToDo":
									if not frappe.db.has_column(row["reference_type"], "_assign"):
										raise ValueError(
											"Target assignment storage must exist before rehearsal"
										)
									ensure_actor(con, row["allocated_to"])
									ensure_actor(con, row["assigned_by"])
								if dt == "Activity Detail":
									insert_history(row)
								else:
									frappe.get_doc({"doctype": dt, **row}).insert(set_name=row["name"])
								expected.append((dt, row))
								counts[dt] += 1
					for dt, row in expected:
						findings.update(f"{dt}.{f} {v}" for f, v in row_differences(dt, row).items())
					results[code] = {"rows": dict(counts), "findings": dict(findings)}
					frappe.db.rollback()
					assert not frappe.db.exists("Person", {"context_tech_id": ctx})
			return results
		finally:
			frappe.db.rollback()
			frappe.destroy()


if __name__ == "__main__":
	p = argparse.ArgumentParser(description=__doc__)
	p.add_argument("--source-site", required=True)
	p.add_argument("--target", required=True)
	p.add_argument("--evidence", required=True, type=Path)
	args = p.parse_args()
	out = args.evidence.resolve()
	os.chdir(BENCH / "sites")
	result = exercise(args.source_site, args.target)
	with out.open("x") as h:
		json.dump(result, h, indent=2)
	print(json.dumps(result))
