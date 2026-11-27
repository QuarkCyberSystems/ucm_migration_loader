"""Correct an imported field through UCM after a caseworker has edited the live case."""

import argparse
import json
import os
import tempfile
from pathlib import Path
from uuid import uuid4

import frappe
from generator_caseworker_surfaces import published_items
from generator_snapshot import BENCH
from rehearsal_bench import caseworker_session

from ucm_migration_loader import api
from ucm_migration_loader.publication_jobs import request as operator_command


def exercise(run, work):
	from ucm.tests.utils import make_user

	report = api.report(run)
	assert report["state"] == "Activated"
	ctx = report["reconciliation"]["context"]
	row = next(
		item["row"]
		for item in published_items(run)
		if item["doctype"] == "Case" and not item["row"].get("case_tags")
	)
	user = make_user(f"correction-{uuid4().hex}@example.invalid", [(ctx, "Write")])
	frappe.db.commit()
	try:
		with caseworker_session(user, work) as request:
			endpoint = "/api/resource/Case/" + row["name"]
			original = request("GET", endpoint)["data"]
			assert original["case_name"] == row["case_name"]
			remark = "Follow-up recorded during the correction rehearsal " + uuid4().hex
			request("PUT", endpoint, json={"case_remark": remark})
			try:
				operator_command(run, "discard")
			except ValueError as exc:
				assert "Cannot discard a run in Activated" in str(exc)
			else:
				raise AssertionError("An activated delegation accepted a discard")
			finally:
				frappe.db.rollback()
			corrected_name = "Corrected synthetic case " + uuid4().hex
			request("PUT", endpoint, json={"case_name": corrected_name})
			final = request("GET", endpoint)["data"]
			assert final["case_name"] == corrected_name and final["case_remark"] == remark
			assert final["name"] == original["name"] and final["modified_by"] == user
			assert api.report(run) == report
			return {
				"run": run,
				"live_discard_refused": True,
				"caseworker_edit_preserved": True,
				"corrected_field_verified": True,
				"editor_attributed": True,
				"activation_receipt_preserved": True,
			}
	finally:
		frappe.db.rollback()
		account = frappe.get_doc("User", user)
		account.enabled = 0
		account.save()
		frappe.db.commit()


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--site", required=True)
	parser.add_argument("--run", required=True)
	parser.add_argument("--evidence", required=True, type=Path)
	args = parser.parse_args()
	output = args.evidence.resolve()
	assert args.site.startswith("ucm-full-wave-") and args.site.endswith(".localhost")
	assert not output.exists()
	os.chdir(BENCH / "sites")
	frappe.init(site=args.site)
	frappe.connect()
	try:
		with tempfile.TemporaryDirectory() as tmp:
			result = exercise(args.run, Path(tmp))
		with output.open("x") as handle:
			json.dump({"site": args.site, **result}, handle, indent=2)
	finally:
		frappe.destroy()
