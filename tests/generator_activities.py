"""Rehearse published activities in dependency order on the prepared, hidden contexts."""

import argparse
import json
import os
from collections import Counter
from pathlib import Path

import frappe
from generator_collisions import read_site
from generator_snapshot import BENCH

from ucm_migration_loader.activities import documents, insert_activity
from ucm_migration_loader.shared_catalogs import apply_catalogs


def exercise(source, target):
	if source == target or not target.startswith("ucm-full-wave-") or not target.endswith(".localhost"):
		raise ValueError("Select an isolated full-wave rehearsal target")
	with read_site(source) as conn:
		with conn.cursor() as cur:
			cur.execute("select name,context_code from tabContext order by context_code")
			contexts = cur.fetchall()
		frappe.init(site=target)
		frappe.connect()
		try:
			crosswalk = {
				(r["doctype"], r["published"]): r["target"] for r in apply_catalogs(conn)["crosswalk"]
			}
			results = {}
			for ctx, code in contexts:
				count = objectives = batches = 0
				findings = Counter()
				rejected = Counter()
				unavailable = set()
				source_count = 0
				for batch in documents(conn, ctx, 25):
					for parent, children in batch:
						source_count += 1
						if any(
							parent[f] in unavailable
							for f in (
								"activity_service_tech_id",
								"activity_associated_tech_id",
							)
						):
							rejected["failed_prerequisite"] += 1
							unavailable.add(parent["name"])
							continue
						frappe.db.savepoint("activity_check")
						try:
							differences = insert_activity(
								parent,
								children,
								crosswalk,
								source=conn,
								collect_findings=True,
							)
						except frappe.ValidationError as exc:
							frappe.db.rollback(save_point="activity_check")
							reason = (
								"required_place_missing"
								if str(exc) == "A place is required for this activity type"
								else type(exc).__name__
							)
							rejected[reason] += 1
							unavailable.add(parent["name"])
							continue
						findings.update(f"{field}: {kind}" for field, kind in differences.items())
						count += 1
						objectives += len(children)
					batches += 1
				results[code] = {
					"source_activities": source_count,
					"validated_activities": count,
					"rejected": dict(rejected),
					"objectives": objectives,
					"batches": batches,
					"reconciliation_findings": dict(findings),
				}
				frappe.db.rollback()
				assert not frappe.db.exists("Activity", {"context_tech_id": ctx})
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
	with out.open("x") as f:
		json.dump(result, f, indent=2)
	print(json.dumps(result))
