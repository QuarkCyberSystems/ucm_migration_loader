"""What a run keeps for its form and for the list of runs: the time of the steps outside the batches, the list's
columns, and that keeping them never fails a job."""

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import get_datetime

from ucm_migration_loader import constants, publication_jobs, runs
from ucm_migration_loader.tests.utils import REPORT, hidden_context, make_receipt, make_run
from ucm_migration_loader.ucm_migration_loader import run_overview


def column(run: str, field: str):
	return frappe.db.get_value(constants.RUN, run, field)


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
		frappe.db.set_value(constants.RUN, run, "write_plan_json", json.dumps(plan))
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


class TestStalledRun(IntegrationTestCase):
	"""A job whose process was killed, or whose worker went down, changes nothing on the run: the form finds out
	from the queue that no job is on it."""

	def quiet(self, state="Publication applying", minutes=5, **values):
		from frappe.utils import add_to_date, now_datetime

		seen = add_to_date(now_datetime(), minutes=-minutes)
		return make_run(state=state, worker_seen=seen, worker_phase="Writing Person", **values)

	def shown(self, run) -> dict:
		return run_overview.build(frappe.get_doc(constants.RUN, run.name))

	def test_a_load_without_a_job_is_shown_as_stopped(self):
		shown = self.shown(self.quiet(applied=1))
		self.assertTrue(shown["stalled"])
		self.assertEqual(shown["batches_committed"], 1)
		statuses = {step["step"]: step["status"] for step in shown["steps"]}
		self.assertEqual(statuses["write"], "no job")
		self.assertEqual(statuses["verify"], "waiting")

	def test_a_discard_without_a_job_is_shown_as_stopped(self):
		shown = self.shown(self.quiet(state="Publication cleaning"))
		self.assertTrue(shown["stalled"])
		self.assertEqual(shown["steps"][-1]["step"], "discard")
		self.assertEqual(shown["steps"][-1]["status"], "no job")

	def test_a_load_with_a_job_in_the_queue_is_at_work(self):
		run = self.quiet()
		with patch.object(run_overview, "working", return_value=True):
			shown = self.shown(run)
		self.assertFalse(shown["stalled"])
		self.assertIn("running", [step["status"] for step in shown["steps"]])

	def test_a_worker_heard_of_just_now_is_not_looked_up(self):
		"""Between two jobs of a load the queue may be empty for a moment."""
		run = self.quiet(minutes=0)
		with patch.object(run_overview, "working", return_value=False) as looked_up:
			self.assertFalse(self.shown(run)["stalled"])
		looked_up.assert_not_called()

	def test_a_queue_that_cannot_be_read_says_nothing(self):
		run = self.quiet()
		with patch("frappe.utils.background_jobs.get_queue", side_effect=RuntimeError("no redis")):
			self.assertIsNone(run_overview.working(run.name))
			self.assertFalse(self.shown(run)["stalled"])

	def test_a_run_that_waits_for_a_person_is_never_stopped(self):
		for state in ("Publication staged", "Publication paused", "Publication ready", "Activated"):
			self.assertFalse(self.shown(self.quiet(state=state))["stalled"], state)

	def test_the_queue_is_searched_for_the_runs_own_jobs(self):
		from types import SimpleNamespace

		run, other = self.quiet(), self.quiet()

		def job(name, site=None):
			kwargs = {"site": site or frappe.local.site, "kwargs": {"run": name, "action": "apply"}}
			return SimpleNamespace(kwargs=kwargs)

		jobs = {"waiting": job(other.name), "elsewhere": job(run.name, site="another.site"), "gone": None}
		queue = SimpleNamespace(
			get_job_ids=lambda: ["waiting", "gone"],
			started_job_registry=SimpleNamespace(get_job_ids=lambda: ["elsewhere"]),
			fetch_job=lambda job_id: jobs[job_id],
		)
		with patch("frappe.utils.background_jobs.get_queue", return_value=queue):
			self.assertIs(run_overview.working(run.name), False)
			self.assertIs(run_overview.working(other.name), True)
			jobs["started"] = job(run.name)
			queue.started_job_registry = SimpleNamespace(get_job_ids=lambda: ["started"])
			self.assertIs(run_overview.working(run.name), True)


class TestFailedJob(IntegrationTestCase):
	def test_a_job_that_lost_its_connection_takes_a_new_one_to_say_so(self):
		with (
			patch.object(frappe.db, "rollback", side_effect=frappe.db.OperationalError(2006, "gone away")),
			patch.object(frappe.db, "close") as closed,
			patch.object(frappe.db, "connect") as connected,
		):
			publication_jobs._roll_back()
		closed.assert_called_once()
		connected.assert_called_once()

	def test_a_job_that_kept_its_connection_only_rolls_back(self):
		with (
			patch.object(frappe.db, "rollback") as rolled_back,
			patch.object(frappe.db, "connect") as connected,
		):
			publication_jobs._roll_back()
		rolled_back.assert_called_once()
		connected.assert_not_called()

	def test_a_failed_apply_pauses_the_run_with_what_failed(self):
		context = hidden_context("LDS4")
		run = make_run(state="Publication applying", context=context, dispatch_id=4, applied=1).name
		with (
			patch.object(frappe.db, "commit"),
			patch.object(publication_jobs, "set_based", side_effect=frappe.db.OperationalError(2013, "lost")),
			patch.object(publication_jobs, "_roll_back"),
		):
			self.assertRaises(frappe.db.OperationalError, publication_jobs.execute, run, "apply", 4, 5)
		self.assertEqual(column(run, "state"), "Publication paused")
		self.assertEqual(column(run, "finding"), "apply after committed batch 1: OperationalError")

	def test_a_load_the_site_is_not_ready_for_says_what_is_missing(self):
		"""In a sentence that names a DocType. Any other failure gives its kind only: it may quote a value."""
		context = hidden_context("LDS5")
		run = make_run(state="Publication applying", context=context, dispatch_id=4).name
		missing = runs.Unprepared("A Field Clearance the delegation refers to is not on this site")
		for failure, shown in (
			(missing, str(missing)),
			(ValueError("invalid literal: 'a name'"), "ValueError"),
		):
			frappe.db.set_value(constants.RUN, run, {"state": "Publication applying", "finding": None})
			with (
				patch.object(frappe.db, "commit"),
				patch.object(publication_jobs, "set_based", side_effect=failure),
				patch.object(publication_jobs, "_roll_back"),
			):
				self.assertRaises(ValueError, publication_jobs.execute, run, "apply", 4, 5)
			self.assertEqual(column(run, "state"), "Publication paused")
			self.assertEqual(column(run, "finding"), f"apply after committed batch 0: {shown}")


class TestRouteOfABenchLoad(IntegrationTestCase):
	"""The set-based steps can be called from the bench: the run then carries the route as one chosen in Desk."""

	def test_a_staged_run_is_given_the_route(self):
		from ucm_migration_loader.ucm_migration_loader import set_based

		run = make_run(state="Publication staged", route="Document API").name
		set_based._take_route(run)
		self.assertEqual(column(run, "write_route"), "Set-based SQL")
		self.assertTrue(publication_jobs.set_based(run))
		set_based._take_route(run)  # and keeps it

	def test_a_run_started_on_another_route_is_refused(self):
		from ucm_migration_loader.ucm_migration_loader import set_based

		run = make_run(state="Publication applying", route="Document API", applied=1).name
		self.assertRaisesRegex(ValueError, "another write route", set_based._take_route, run)
		self.assertEqual(column(run, "write_route"), None)
