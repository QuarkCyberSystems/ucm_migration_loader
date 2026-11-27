"""What a run keeps for its form and for the list of runs: the time of the steps outside the batches, the list's
columns, and that keeping them never fails a job."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import get_datetime

from ucm_migration_loader import api, publication_jobs
from ucm_migration_loader.tests.utils import REPORT, hidden_context, make_receipt, make_run
from ucm_migration_loader.ucm_migration_loader import run_overview


def column(run: str, field: str):
	return frappe.db.get_value(api.RUN, run, field)


class TestRunSummary(IntegrationTestCase):
	def test_the_seconds_of_a_step_done_over_several_jobs_add_up(self):
		run = make_run().name
		run_overview.note(run, "discard", 1.5)
		run_overview.note(run, "discard", 2.25, batches=3)
		timeline = json.loads(column(run, "timeline_json"))
		self.assertEqual(timeline["discard"]["seconds"], 3.75)
		self.assertEqual(timeline["discard"]["batches"], 3)
		self.assertIn("at", timeline["discard"])

	def test_a_note_leaves_the_other_steps_and_the_runs_time(self):
		run = make_run()
		run_overview.note(run.name, "stage", 4.0, business_batches=2)
		run_overview.note(run.name, "activate", 6.0)
		timeline = json.loads(column(run.name, "timeline_json"))
		self.assertEqual(timeline["stage"]["seconds"], 4.0)
		self.assertEqual(timeline["activate"]["seconds"], 6.0)
		self.assertEqual(column(run.name, "modified"), get_datetime(run.modified))

	def test_the_list_shows_the_delegation_by_name_and_code(self):
		context = hidden_context("LDS1")
		run = make_run(context=context).name
		run_overview.refresh(run)
		self.assertEqual(column(run, "delegation"), "Test LDS1 (LDS1)")

	def test_a_context_the_site_does_not_hold_is_shown_by_its_id(self):
		run = make_run()
		run_overview.refresh(run.name)
		self.assertEqual(column(run.name, "delegation"), run.context_id)

	def test_the_duration_is_the_time_the_load_worked(self):
		run = make_run(report=REPORT).name
		make_receipt(run, 1, write_route="Set-based SQL", parent_rows=100, write_seconds=20.0)
		make_receipt(run, 2, write_route="Set-based SQL", parent_rows=300, write_seconds=30.4)
		run_overview.note(run, "stage", 4.0, business_batches=2)
		run_overview.note(run, "discard", 100.0)
		run_overview.refresh(run)
		# stage 4 + write 50.4 + derive 5 + verify 6; a discard is not part of the load
		self.assertEqual(column(run, "duration"), 65)
		self.assertEqual(column(run, "write_route"), "Set-based SQL")

	def test_a_run_without_a_measured_step_shows_no_duration(self):
		run = make_run(state="Publication staged").name
		run_overview.refresh(run)
		self.assertIsNone(column(run, "duration"))

	def test_a_refused_run_shows_no_route(self):
		run = make_run(state="Rejected", route=None).name
		run_overview.refresh(run)
		self.assertIsNone(column(run, "write_route"))

	def test_routes_chosen_per_doctype_are_named_so(self):
		run = make_run(state="Publication staged").name
		plan = {"Person": "Document API", "Activity": "Validated bulk SQL"}
		frappe.db.set_value(api.RUN, run, "write_plan_json", json.dumps(plan))
		run_overview.refresh(run)
		self.assertEqual(column(run, "write_route"), "By DocType")

	def test_refreshing_the_list_leaves_the_runs_time(self):
		run = make_run(report=REPORT)
		run_overview.refresh(run.name)
		self.assertEqual(column(run.name, "modified"), get_datetime(run.modified))


class TestJobSummary(IntegrationTestCase):
	"""A queued job ends by keeping the run's summary. The job commits there: in a test it does not."""

	def setUp(self):
		super().setUp()
		commit = patch.object(frappe.db, "commit")
		self.commit = commit.start()
		self.addCleanup(commit.stop)

	def test_an_activation_keeps_its_time(self):
		run = make_run(state="Activated", report=REPORT).name
		publication_jobs._keep_summary(run, "activate", 12.34)
		timeline = json.loads(column(run, "timeline_json"))
		self.assertEqual(timeline["activate"]["seconds"], 12.34)
		self.assertEqual(column(run, "duration"), 23)  # derive 5 + verify 6 + activate 12.34
		self.commit.assert_called_once()

	def test_an_apply_job_keeps_no_time_of_its_own(self):
		"""The batches' receipts hold the time of an apply."""
		run = make_run(state="Publication applying").name
		publication_jobs._keep_summary(run, "apply", 50.0)
		self.assertIsNone(column(run, "timeline_json"))

	def test_a_job_that_did_not_hold_the_run_keeps_no_time(self):
		context = hidden_context("LDS2")
		run = make_run(state="Publication cleaning", context=context, dispatch_id=7).name
		publication_jobs.execute(run, "discard", ticket=6, max_batches=5)
		self.assertIsNone(column(run, "timeline_json"))
		self.assertEqual(column(run, "state"), "Publication cleaning")
		self.assertEqual(column(run, "delegation"), "Test LDS2 (LDS2)")

	def test_a_failing_summary_does_not_fail_the_job(self):
		run = make_run(state="Activated", report=REPORT).name
		with (
			patch.object(run_overview, "refresh", side_effect=RuntimeError("boom")),
			patch.object(frappe.db, "rollback") as rollback,
			patch.object(frappe, "log_error") as logged,
		):
			publication_jobs._keep_summary(run, "activate", 1.0)
		rollback.assert_called_once()
		logged.assert_called_once()
		self.commit.assert_not_called()

	def test_a_job_keeps_the_summary_also_when_its_work_fails(self):
		context = hidden_context("LDS3")
		run = make_run(state="Publication cleaning", context=context, dispatch_id=3).name
		with (
			patch.object(publication_jobs, "_execute", side_effect=RuntimeError("the work failed")),
			patch.object(publication_jobs, "_keep_summary") as kept,
		):
			self.assertRaises(RuntimeError, publication_jobs.execute, run, "discard", 3, 5)
		kept.assert_called_once_with(run, "discard", None)
