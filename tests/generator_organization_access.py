"""Verify migrated organization clearance and exemption behavior through authenticated HTTPS."""

import argparse
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from uuid import uuid4

import frappe
from generator_held_shares import assert_denied
from generator_snapshot import BENCH
from rehearsal_bench import login_caseworker, tls_site

from ucm_migration_loader.publication_receiver import local_publication


def example(code):
	context = frappe.db.get_value("Context", {"context_code": code}, "name")
	run = frappe.get_doc("UCM Migration Run", {"context_id": context, "state": "Activated"})
	with sqlite3.connect(local_publication(run.name).resolve().as_uri() + "?mode=ro", uri=True) as db:
		for (payload,) in db.execute("select payload from batches order by sequence"):
			if b'"organization_financing"' not in payload:
				continue
			for item in json.loads(payload):
				row = item["row"]
				if (
					item["doctype"] == "Sensitive Information"
					and row["record_doctype"] == "Organization"
					and row["organization_financing"]
				):
					return context, row
	raise ValueError("Missing generated organization with a populated protected field")


def read_profile(request, row, allowed):
	resource = "Sensitive Information"
	entries = request(
		"GET",
		"/api/resource/" + resource,
		params={
			"filters": json.dumps({"name": row["name"]}),
			"fields": json.dumps(["name", "organization_financing"]),
		},
	)["data"]
	if not allowed:
		assert not entries
		assert_denied(request, resource, row["name"])
		return
	assert entries == [{"name": row["name"], "organization_financing": row["organization_financing"]}]
	doc = request("GET", "/api/resource/" + resource + "/" + row["name"])["data"]
	assert doc["organization_financing"] == row["organization_financing"]


def exercise(work):
	from ucm.tests.utils import make_user

	bol, protected = example("BOL")
	cha, exempt = example("CHA")
	users = []
	try:
		with tls_site(frappe.local.site, work) as (url, cert):
			for ctx, row, other in ((bol, protected, exempt), (cha, exempt, protected)):
				user = make_user(f"org-{uuid4().hex}@example.invalid", [(ctx, "Read")])
				users.append(user)
				frappe.db.commit()
				with login_caseworker(user, url, cert) as request:
					assert (
						request("GET", "/api/resource/Organization/" + row["record_name"])["data"]["name"]
						== row["record_name"]
					)
					read_profile(request, row, ctx == cha)
					read_profile(request, other, False)
					if ctx == bol:
						team = frappe.db.get_value("Team", {"team_name": f"Contexts of {user}"}, "name")
						profile = frappe.get_doc(
							"Team Security Profile",
							{"team_tech_id": team, "context_tech_id": ctx},
						)
						clearance = frappe.db.get_value(
							"Sensitive Information",
							row["name"],
							"field_clearance_tech_id",
						)
						profile.append(
							"profile_field_clearances",
							{"field_clearance_tech_id": clearance, "can_read": 1},
						)
						profile.save()
						frappe.db.commit()
						read_profile(request, row, True)
		return {
			"BOL_denied_without_clearance": True,
			"BOL_read_with_clearance": True,
			"CHA_read_under_published_exemption": True,
			"cross_context_denied_without_clearance": True,
			"authorized_values_match_publication": True,
			"verified_endpoints": ["resource list", "resource document"],
		}
	finally:
		frappe.db.rollback()
		for user in users:
			account = frappe.get_doc("User", user)
			account.enabled = 0
			account.save()
		frappe.db.commit()


def exercise_hidden(code, work):
	from ucm.tests.utils import make_user

	ctx = frappe.db.get_value("Context", {"context_code": code}, "name")
	assert frappe.db.get_value("Context", ctx, "context_status") == "Locked"
	key = frappe.db.get_value("Organization", {"context_tech_id": ctx}, "name")
	assert key
	private_key = frappe.db.get_value(
		"Sensitive Information",
		{"record_doctype": "Organization", "context_tech_id": ctx},
		"name",
	)
	assert private_key
	user = make_user(f"hidden-org-{uuid4().hex}@example.invalid", [(ctx, "Read")])
	frappe.db.commit()
	try:
		with (
			tls_site(frappe.local.site, work) as (url, cert),
			login_caseworker(user, url, cert) as request,
		):
			assert_denied(request, "Organization", key)
			assert_denied(request, "Sensitive Information", private_key)
			assert (
				request(
					"GET",
					"/api/resource/Sensitive Information",
					params={"filters": json.dumps({"name": private_key})},
				)["data"]
				== []
			)
			assert (
				request(
					"GET",
					"/api/resource/Organization",
					params={"filters": json.dumps({"name": key})},
				)["data"]
				== []
			)
		frappe.db.rollback()
		assert frappe.db.get_value("Context", ctx, "context_status") == "Locked"
		return {
			"context_code": code,
			"organization_committed": True,
			"protected_organization_committed_and_hidden": True,
			"assigned_caseworker_denied_list_and_document": True,
		}
	finally:
		account = frappe.get_doc("User", user)
		account.enabled = 0
		account.save()
		frappe.db.commit()


if __name__ == "__main__":
	p = argparse.ArgumentParser(description=__doc__)
	p.add_argument("--hidden-context")
	p.add_argument("--site", required=True)
	p.add_argument("--evidence", required=True, type=Path)
	args = p.parse_args()
	assert args.site.startswith("ucm-full-wave-") and args.site.endswith(".localhost")
	output = args.evidence.resolve()
	os.chdir(BENCH / "sites")
	frappe.init(site=args.site)
	frappe.connect()
	try:
		with tempfile.TemporaryDirectory() as tmp:
			result = (
				exercise_hidden(args.hidden_context, Path(tmp))
				if args.hidden_context
				else exercise(Path(tmp))
			)
		with output.open("x") as handle:
			json.dump({"site": args.site, **result}, handle, indent=2)
		print(json.dumps(result))
	finally:
		frappe.destroy()
