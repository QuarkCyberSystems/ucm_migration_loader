"""The hand-over of a run's metrics to the migration side: who may read them, what they hold, and that the run
records a hand-over only of the metrics it holds now."""

import json

import frappe
from frappe.tests import IntegrationTestCase

from ucm_migration_loader import api
from ucm_migration_loader.contract import digest
from ucm_migration_loader.tests.utils import FOUND, REPORT, make_account, make_run
from ucm_migration_loader.ucm_migration_loader import data_quality


class TestQualityAPI(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.publisher = make_account("publisher")
		self.other = make_account("other-publisher")
		self.run_name = make_run(report=REPORT, publishing_user=self.publisher).name

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_the_metrics_carry_the_run_and_their_own_digest(self):
		document = api.quality(self.run_name)
		self.assertEqual(document["format"], data_quality.FORMAT)
		self.assertEqual((document["run"], document["site"]), (self.run_name, frappe.local.site))
		self.assertEqual(document["state"], "Publication ready")
		self.assertEqual(document["sha256"], digest(document["metrics"]))
		self.assertTrue(all(row["run"] == self.run_name for row in document["metrics"]))
		persons = [
			row
			for row in document["metrics"]
			if (row["metric"], row["entity"]) == ("Number of records", "Person")
		]
		self.assertEqual([(row["source_value"], row["target_value"]) for row in persons], [(100, 100)])
		self.assertFalse([row for row in document["metrics"] if row["passed"] is False])

	def test_the_same_report_gives_the_same_rows(self):
		self.assertEqual(api.quality(self.run_name), api.quality(self.run_name))

	def test_findings_fail_their_metrics(self):
		run = make_run(state="Publication paused", report=FOUND).name
		failed = {
			(row["metric"], row["entity"], row["rule"])
			for row in api.quality(run)["metrics"]
			if row["passed"] is False
		}
		self.assertIn(("Number of records", "Person", ""), failed)
		self.assertIn(("Integrity rule broken", "Activity", "activity_status: not an option"), failed)
		self.assertIn(("Errors count by severity", "", "CRITICAL"), failed)

	def test_the_publishing_account_reads_its_own_run_only(self):
		foreign = make_run(report=REPORT, publishing_user=self.other).name
		frappe.set_user(self.publisher)
		self.assertEqual(api.quality(self.run_name)["run"], self.run_name)
		self.assertRaises(frappe.PermissionError, api.quality, foreign)
		self.assertRaises(frappe.PermissionError, api.status, foreign)
		self.assertRaises(frappe.PermissionError, api.report, foreign)
		self.assertRaises(frappe.PermissionError, api.quality_received, foreign, "0" * 64, 1, "file x.csv")

	def test_an_account_that_published_nothing_reads_no_run(self):
		frappe.set_user(make_account("bystander"))
		self.assertRaises(frappe.PermissionError, api.quality, self.run_name)
		frappe.set_user("Guest")
		self.assertRaises(frappe.PermissionError, api.quality, self.run_name)

	def test_an_operator_reads_every_run(self):
		frappe.set_user(make_account("operator", operator=True))
		self.assertEqual(api.quality(self.run_name)["run"], self.run_name)

	def test_a_run_without_a_report_has_no_metrics(self):
		run = make_run(state="Publication staged").name
		self.assertRaisesRegex(ValueError, "no verification report", api.quality, run)

	def test_a_run_that_is_no_sealed_publication_has_no_metrics(self):
		run = make_run(report=REPORT).name
		frappe.db.set_value(api.RUN, run, "manifest_json", json.dumps({"profile": "earlier"}))
		self.assertRaisesRegex(ValueError, "sealed publications", api.quality, run)

	def test_the_run_records_the_hand_over(self):
		before = frappe.db.get_value(api.RUN, self.run_name, "modified")
		frappe.set_user(self.publisher)
		document = api.quality(self.run_name)
		receipt = api.quality_received(
			self.run_name, document["sha256"], len(document["metrics"]), "table ucm_target_metric"
		)
		frappe.set_user("Administrator")
		kept = json.loads(frappe.db.get_value(api.RUN, self.run_name, "quality_json"))
		self.assertEqual(kept, receipt)
		self.assertEqual(
			(kept["rows"], kept["sha256"], kept["destination"], kept["by"]),
			(len(document["metrics"]), document["sha256"], "table ucm_target_metric", self.publisher),
		)
		# the hand-over is not a step of the load: the run keeps the time of its last step, and so do its metrics
		self.assertEqual(frappe.db.get_value(api.RUN, self.run_name, "modified"), before)
		self.assertEqual(api.quality(self.run_name), document)

	def hand_over_step(self) -> dict:
		from ucm_migration_loader.ucm_migration_loader import run_overview

		shown = run_overview.build(frappe.get_doc(api.RUN, self.run_name))
		return next(step for step in shown["steps"] if step["step"] == "hand over")

	def test_the_form_shows_the_hand_over_until_the_run_changes(self):
		self.assertEqual(self.hand_over_step()["status"], "waiting")
		document = api.quality(self.run_name)
		api.quality_received(self.run_name, document["sha256"], len(document["metrics"]), "file m.csv")
		step = self.hand_over_step()
		self.assertEqual((step["status"], step["facts"]["destination"]), ("done", "file m.csv"))
		# the delegation is activated: the access rows are released, and the metrics say so
		api.mark_activated(self.run_name)
		step = self.hand_over_step()
		self.assertEqual((step["status"], step["facts"]["current"]), ("waiting", False))
		self.assertEqual(step["facts"]["destination"], "file m.csv")
		document = api.quality(self.run_name)
		api.quality_received(self.run_name, document["sha256"], len(document["metrics"]), "file again.csv")
		step = self.hand_over_step()
		self.assertEqual((step["status"], step["facts"]["destination"]), ("done", "file again.csv"))

	def test_a_hand_over_can_be_repeated(self):
		document = api.quality(self.run_name)
		for destination in ("file first.csv", "file second.csv"):
			api.quality_received(self.run_name, document["sha256"], len(document["metrics"]), destination)
		kept = json.loads(frappe.db.get_value(api.RUN, self.run_name, "quality_json"))
		self.assertEqual(kept["destination"], "file second.csv")

	def test_metrics_read_before_the_run_changed_are_not_recorded(self):
		document = api.quality(self.run_name)
		frappe.db.set_value(api.RUN, self.run_name, "reconciliation_json", json.dumps(FOUND))
		self.assertRaisesRegex(
			ValueError,
			"changed since these metrics were read",
			api.quality_received,
			self.run_name,
			document["sha256"],
			len(document["metrics"]),
			"file stale.csv",
		)
		self.assertIsNone(frappe.db.get_value(api.RUN, self.run_name, "quality_json"))

	def test_a_wrong_row_count_is_not_recorded(self):
		document = api.quality(self.run_name)
		self.assertRaises(
			ValueError,
			api.quality_received,
			self.run_name,
			document["sha256"],
			len(document["metrics"]) - 1,
			"file short.csv",
		)
		self.assertIsNone(frappe.db.get_value(api.RUN, self.run_name, "quality_json"))

	def test_a_long_destination_is_cut(self):
		document = api.quality(self.run_name)
		receipt = api.quality_received(
			self.run_name, document["sha256"], len(document["metrics"]), "file " + "x" * 400
		)
		self.assertEqual(len(receipt["destination"]), 140)
