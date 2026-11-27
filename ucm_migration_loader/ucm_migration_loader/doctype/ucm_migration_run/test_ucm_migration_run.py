"""A migration run in the Desk: who may open it, the number it is given, and what its form shows in each state."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ucm_migration_loader import api
from ucm_migration_loader.tests.utils import FOUND, REPORT, make_account, make_receipt, make_run
from ucm_migration_loader.ucm_migration_loader import run_overview
from ucm_migration_loader.ucm_migration_loader.doctype.ucm_migration_run.ucm_migration_run import overview

# a run links to the accounts that published and activated it: the tests make their own
IGNORE_TEST_RECORD_DEPENDENCIES = ["User"]


def statuses(shown: dict) -> dict:
	return {step["step"]: step["status"] for step in shown["steps"]}


def step(shown: dict, name: str) -> dict:
	return next(step for step in shown["steps"] if step["step"] == name)


class TestUCMMigrationRun(IntegrationTestCase):
	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_a_run_is_given_a_number(self):
		first, second = make_run(), make_run()
		self.assertRegex(first.display_id, r"^MIG-\d{4,}$")
		self.assertEqual(int(second.display_id[4:]), int(first.display_id[4:]) + 1)

	def test_only_an_operator_opens_a_run(self):
		publisher = make_account("publisher")
		run = make_run(report=REPORT, publishing_user=publisher).name
		frappe.set_user(publisher)
		self.assertRaises(frappe.PermissionError, overview, run)
		self.assertFalse(frappe.has_permission(api.RUN, "read", run))
		frappe.set_user(make_account("operator", operator=True))
		self.assertTrue(overview(run)["publication"])
		self.assertTrue(frappe.has_permission(api.RUN, "read", run))

	def test_an_operator_cannot_write_a_run_by_hand(self):
		"""The loader's own methods write a run; the Desk only reads it."""
		run = make_run().name
		frappe.set_user(make_account("operator", operator=True))
		for right in ("write", "create", "delete"):
			self.assertFalse(frappe.has_permission(api.RUN, right, run), right)

	def test_a_run_that_is_no_sealed_publication_has_no_overview(self):
		run = make_run().name
		frappe.db.set_value(api.RUN, run, "manifest_json", json.dumps({"profile": "earlier"}))
		self.assertEqual(overview(run), {"publication": False})

	def test_a_verified_set_based_run_waits_for_the_decision(self):
		run = make_run(report=REPORT).name
		make_receipt(run, 1, write_route="Set-based SQL", parent_rows=100, write_seconds=20.0)
		make_receipt(run, 2, write_route="Set-based SQL", parent_rows=300, write_seconds=30.0)
		shown = overview(run)
		self.assertEqual(
			statuses(shown),
			{
				"stage": "done",
				"write": "done",
				"derive": "done",
				"verify": "done",
				"hand over": "waiting",
				"activate": "decision",
			},
		)
		write = step(shown, "write")
		self.assertEqual(
			(write["seconds"], write["facts"]["batches"], write["facts"]["parents"]), (50.0, 2, 400)
		)
		self.assertEqual(write["facts"]["by_route"], {"Set-based SQL": 400})
		self.assertEqual(step(shown, "derive")["seconds"], 5.0)
		self.assertEqual(step(shown, "verify")["facts"]["rules"], 40)
		self.assertEqual(step(shown, "activate")["facts"]["deferred"], {"ToDo": 4, "Record Share": 10})
		self.assertEqual(shown["findings"], [])
		rows = {row["doctype"]: row for row in shown["records"]}
		self.assertEqual((rows["Person"]["published"], rows["Person"]["stored"]), (100, 100))
		self.assertTrue(rows["Record Share"]["deferred"])
		self.assertIsNone(rows["Record Share"]["stored"])

	def test_a_document_route_run_has_no_derive_step(self):
		"""The receipts of the document route carry its times: written batch by batch, read back batch by batch."""
		report = {
			"published_rows": REPORT["published_rows"],
			"checked_business_rows": {"Person": 100, "Activity": 300},
			"committed_business_batches": 2,
		}
		run = make_run(report=report, route="Document API", reconcile_cursor=2).name
		make_receipt(
			run, 1, parent_rows=100, write_seconds=60.0, validation_seconds=5.0, reconcile_seconds=9.0
		)
		make_receipt(
			run, 2, parent_rows=300, write_seconds=120.0, attachment_seconds=2.5, reconcile_seconds=11.0
		)
		shown = overview(run)
		self.assertEqual(list(statuses(shown)), ["stage", "write", "verify", "hand over", "activate"])
		self.assertEqual(shown["facts"]["route"], "Document API")
		write, verify = step(shown, "write"), step(shown, "verify")
		self.assertEqual(write["seconds"], 187.5)
		self.assertEqual(write["facts"]["by_route"], {"Document API": 400})
		self.assertEqual((verify["status"], verify["seconds"]), ("done", 20.0))
		self.assertEqual(verify["facts"]["rows"], 400)
		rows = {row["doctype"]: row for row in shown["records"]}
		self.assertEqual(rows["Activity"]["stored"], 300)
		# its metrics hold the counts and the times; the set-based checks are not claimed
		metrics = api.quality(run)["metrics"]
		names = {row["metric"] for row in metrics}
		self.assertIn("Number of records", names)
		self.assertNotIn("Integrity rules broken", names)
		self.assertNotIn("Exact match per field", names)
		times = {row["rule"]: row["value"] for row in metrics if row["metric"] == "Migration time, seconds"}
		self.assertEqual(times, {"write": 187.5, "verify": 20.0})

	def test_a_run_that_is_loading_shows_the_step_it_is_in(self):
		run = make_run(state="Publication applying", reconcile_counts=json.dumps({"derived": {"x": 1}})).name
		run_overview.note(run, "stage", 3.0, business_batches=2)
		make_receipt(run, 1, write_route="Set-based SQL", parent_rows=100, write_seconds=20.0)
		make_receipt(run, 2, write_route="Set-based SQL", parent_rows=300, write_seconds=30.0)
		self.assertEqual(
			statuses(overview(run)),
			{"stage": "done", "write": "done", "derive": "done", "verify": "running", "activate": "waiting"},
		)

	def test_a_failed_verification_shows_its_findings_by_severity(self):
		run = make_run(state="Publication paused", report=FOUND).name
		shown = overview(run)
		self.assertEqual(statuses(shown)["verify"], "failed")
		self.assertEqual(statuses(shown)["activate"], "waiting")
		self.assertEqual(
			[(row["severity"], row["doctype"], row["check"], row["found"]) for row in shown["findings"]],
			[("CRITICAL", "Person", "rows missing in UCM", 1), ("HIGH", "Activity", "rule broken", 5)],
		)

	def test_an_activated_run_shows_who_opened_it_and_what_was_released(self):
		operator = make_account("operator", operator=True)
		report = {
			**REPORT,
			"released_access_rows": {"ToDo": 4, "Record Share": 7},
			"shares_held_at_activation": 3,
		}
		run = make_run(
			state="Activated",
			report=report,
			approved_by=operator,
			activated_on="2026-01-02 03:04:05",
			timeline_json=json.dumps({"activate": {"seconds": 12.34}}),
			quality_json=json.dumps({"rows": 69, "destination": "table ucm_target_metric"}),
		).name
		shown = overview(run)
		self.assertEqual(statuses(shown)["hand over"], "done")
		activate = step(shown, "activate")
		self.assertEqual((activate["status"], activate["seconds"]), ("done", 12.3))
		self.assertEqual(activate["facts"]["by"], operator)
		self.assertEqual(activate["facts"]["on"], "2026-01-02 03:04:05")
		self.assertEqual(activate["facts"]["released"], {"ToDo": 4, "Record Share": 7})
		self.assertEqual(activate["facts"]["held"], 3)
		rows = {row["doctype"]: row for row in shown["records"]}
		self.assertEqual((rows["Record Share"]["stored"], rows["Record Share"]["held"]), (7, 3))
		self.assertEqual((rows["ToDo"]["stored"], rows["ToDo"]["held"]), (4, 0))
		# the released rows count as arrived: 7 shares released and 3 kept for delegations not yet live
		shares = [
			row
			for row in api.quality(run)["metrics"]
			if (row["metric"], row["entity"]) == ("Number of records", "Record Share")
		]
		self.assertEqual(
			[(row["source_value"], row["target_value"], row["passed"]) for row in shares], [(10, 10, True)]
		)

	def test_a_discarded_run_shows_the_discard_and_no_decision(self):
		run = make_run(state="Discarded", timeline_json=json.dumps({"discard": {"seconds": 8.0}})).name
		shown = overview(run)
		self.assertNotIn("activate", statuses(shown))
		self.assertNotIn("hand over", statuses(shown))
		self.assertEqual((step(shown, "discard")["status"], step(shown, "discard")["seconds"]), ("done", 8.0))

	def test_the_dashboard_link_is_built_from_the_site_setting(self):
		run = make_run(report=REPORT)
		self.assertIsNone(overview(run.name)["quality_url"])
		setting = {"ucm_migration_quality_url": "https://quality.example/runs?run={run}&context={context}"}
		with patch.dict(frappe.conf, setting):
			url = overview(run.name)["quality_url"]
		self.assertEqual(url, f"https://quality.example/runs?run={run.name}&context={run.context_id}")
