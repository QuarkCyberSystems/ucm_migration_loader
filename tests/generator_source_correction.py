"""Rehearse correcting a reported activity using its original generator value."""

import argparse
import json
import os
import tempfile
from pathlib import Path

import frappe
from generator_snapshot import BENCH, site_connection

from ucm_migration_loader.frozen import freeze, verify_publication


def rehearse(site, work):
	if not site.startswith("ucm-generator-") or not site.endswith(".localhost"):
		raise ValueError("Select a synthetic publication site")
	root = Path(frappe.get_site_path("private", "files")).resolve()
	context = frappe.db.get_value("Context", {"context_code": "BOL"}, "name")
	with site_connection() as conn, conn.cursor() as cur:

		def publish(label, findings=None):
			return freeze(
				conn,
				work / label,
				root,
				"ucm.localhost",
				label,
				context,
				25,
				262144,
				findings,
			)

		baseline = publish("original")
		# Shadow the source table on this connection only. Use the exact generator
		# place as the independent expected correction. Closing drops the copy.
		cur.execute("create temporary table activity_fixture like tabActivity")
		cur.execute("insert into activity_fixture select * from tabActivity")
		cur.execute("alter table activity_fixture rename to tabActivity")
		cur.execute(
			"""select a.name,a.activity_place_tech_id from tabActivity a
            join `tabActivity Type` t on t.name=a.activity_type_tech_id
            where a.context_tech_id=%s and t.activity_type_place_required=1
            and t.activity_type_enabled_place=1 and a.activity_place_tech_id is not null
            order by a.name limit 1""",
			context,
		)
		activity, expected_place = cur.fetchone()
		cur.execute("update tabActivity set activity_place_tech_id=null where name=%s", activity)
		report = work / "findings.jsonl"
		try:
			publish("invalid", report)
		except ValueError as exc:
			assert str(exc) == "1 activity lacks a required place"
		else:
			raise AssertionError("Incomplete source was accepted")
		assert not (work / "invalid").exists()
		(finding,) = [json.loads(line) for line in report.read_text().splitlines()]
		assert (finding["name"], finding["field"], finding["rule"]) == (
			activity,
			"activity_place_tech_id",
			"required_place",
		)
		cur.execute(
			"update tabActivity set activity_place_tech_id=%s where name=%s",
			(expected_place, finding["name"]),
		)
		corrected = publish("corrected", work / "remaining.jsonl")
		assert not (work / "remaining.jsonl").exists()
		assert verify_publication(work / "corrected", corrected["run"])["snapshot"] == "corrected"
		for key in ("counts", "reference_sha256", "batch_chain_sha256"):
			assert corrected[key] == baseline[key], key
		return {
			"source": site,
			"context_code": "BOL",
			"invalid_snapshot_refused": True,
			"verified_finding_count": 1,
			"corrected_snapshot_verified": True,
			"all_batch_bytes_match_original": True,
			"corrected_batches": corrected["batches"],
			"corrected_rows": sum(corrected["counts"].values()),
		}


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--source-site", required=True)
	parser.add_argument("--evidence", required=True, type=Path)
	args = parser.parse_args()
	output = args.evidence.resolve()
	os.chdir(BENCH / "sites")
	frappe.init(site=args.source_site)
	frappe.connect()
	try:
		with tempfile.TemporaryDirectory() as directory:
			result = rehearse(args.source_site, Path(directory))
		with output.open("x") as out:
			json.dump(result, out, indent=2)
		print(json.dumps(result))
	finally:
		frappe.destroy()
