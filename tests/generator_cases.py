"""Exercise generated case scalars and Context Case children through UCM in an isolated rollback-only rehearsal."""

import argparse
import json
import os

import frappe
from generator_collisions import read_site
from generator_snapshot import BENCH, fingerprint

from ucm_migration_loader.api import PILOT_MARKER
from ucm_migration_loader.parent_publication import CASE_FIELDS, case_documents

CHILD_FIELDS = ("name", "idx", "context_tech_id")


def project(doc, fields):
	return [None if doc.get(f) is None else str(doc.get(f)) for f in fields]


def rehearse(source, target):
	if not target.startswith("ucm-") or not target.endswith(".localhost") or source == target:
		raise ValueError("Use a separate local synthetic rehearsal target")
	with read_site(source) as pub:
		with pub.cursor() as cur:
			cur.execute("select name, context_code from tabContext order by context_code")
			contexts = cur.fetchall()
		frappe.init(site=target)
		frappe.connect()
		try:
			for ctx, code in contexts:
				if frappe.db.exists("Context", ctx):
					raise ValueError("Rehearsal requires unused context IDs")
				frappe.get_doc(
					{
						"doctype": "Context",
						"context_name": "Generator rehearsal " + code,
						"context_code": code,
						"context_status": "Locked",
						"context_discoverability": "Hidden",
						"context_description": PILOT_MARKER,
					}
				).insert(set_name=ctx)
			results = {}
			for ctx, code in contexts:
				parents = children = cross_context = 0
				sample_link = None
				for expected in case_documents(pub, ctx):
					name = expected["name"]
					if frappe.db.exists("Case", name):
						raise ValueError("Generated case key already present on target")
					frappe.get_doc(expected).insert(set_name=name, set_child_names=False)
					saved = frappe.get_doc("Case", name)
					assert project(saved, CASE_FIELDS) == project(expected, CASE_FIELDS)
					source_children = expected["case_contexts"]
					for a, b in zip(saved.case_contexts, source_children, strict=True):
						assert project(a, CHILD_FIELDS) == project(b, CHILD_FIELDS)
						assert (
							str(a.parent) == name
							and a.parentfield == "case_contexts"
							and a.parenttype == "Case"
						)
						cross_context += b["context_tech_id"] != ctx
						sample_link = a
					parents += 1
					children += len(source_children)
				results[code] = {
					"cases": parents,
					"context_case_rows": children,
					"cross_context_links": cross_context,
				}
				# Exercise the independent whole-table comparison on the same physical
				# target and transaction. Change only a published link, retaining every ID.
				if sample_link is not None and len(contexts) >= 2:
					connection = frappe.db._conn
					before = fingerprint(connection, "Context Case", ctx, None)
					original = str(sample_link.context_tech_id)
					alternate = next(other for other, _ in contexts if other != original)
					frappe.db.set_value(
						"Context Case",
						sample_link.name,
						"context_tech_id",
						alternate,
						update_modified=False,
					)
					changed = fingerprint(connection, "Context Case", ctx, None)
					assert (
						before["rows"] == changed["rows"]
						and before["audit_fields_sha256"] == changed["audit_fields_sha256"]
					)
					assert before["stored_fields_sha256"] != changed["stored_fields_sha256"]
					frappe.db.set_value(
						"Context Case",
						sample_link.name,
						"context_tech_id",
						original,
						update_modified=False,
					)
					assert before == fingerprint(connection, "Context Case", ctx, None)
					results[code]["changed_link_detected"] = True
			frappe.db.rollback()
			for ctx, _ in contexts:
				assert not frappe.db.exists("Context", ctx)
				assert not frappe.db.exists("Case", {"context_tech_id": ctx})
			return results
		finally:
			frappe.db.rollback()
			frappe.destroy()


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--source-site", required=True)
	parser.add_argument("--target", required=True)
	args = parser.parse_args()
	os.chdir(BENCH / "sites")
	print(
		json.dumps(
			{
				"target": args.target,
				"rolled_back": rehearse(args.source_site, args.target),
			}
		)
	)
