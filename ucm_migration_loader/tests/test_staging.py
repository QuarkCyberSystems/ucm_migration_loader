"""A sealed publication arriving on the site: staged when it fits, and kept as a refused run, with the reason and
without its file, when it does not. A file sent over HTTPS leaves its run readable by the account that sent it."""

import base64
import hashlib
import json
import tempfile
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ucm_migration_loader import api, publication_receiver, publication_transport
from ucm_migration_loader.tests.utils import (
	hidden_context,
	make_account,
	remove_files,
	seal,
	site_schema,
)
from ucm_migration_loader.ucm_migration_loader import run_overview
from ucm_migration_loader.ucm_migration_loader.doctype.ucm_migration_run.ucm_migration_run import overview


class StagingCase(IntegrationTestCase):
	"""A test stages publications of a context of its own, and removes the files they leave."""

	codes = iter(range(1000))

	def setUp(self):
		super().setUp()
		self.directory = self.enterContext(tempfile.TemporaryDirectory())
		self.context = hidden_context(f"LDR{next(self.codes)}")

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def sealed(self, **changes):
		path, run = seal(self.directory, self.context, **changes)
		self.addCleanup(remove_files, run)
		return str(path), run

	def refused(self, path, run):
		"""Stage a publication that the site must refuse. Returns the run it kept. A refusal is committed, as
		the request that brought the file fails: here it stays in the test's transaction."""
		with patch.object(frappe.db, "commit") as commit:
			self.assertRaisesRegex(
				ValueError, "Publication preflight failed", publication_receiver.stage, path, run
			)
		commit.assert_called_once()
		return frappe.get_doc(api.RUN, run)


class TestRefusal(StagingCase):
	def test_tables_that_differ_are_refused(self):
		schema = site_schema()
		schema["Person"] += "\n  `person_shoe_size` int(11)"
		record = self.refused(*self.sealed(schema=schema))
		self.assertEqual(record.state, "Rejected")
		self.assertEqual(record.finding, "Preflight refused: tables differ from this site")
		kept = json.loads(record.reconciliation_json)["preflight"]
		self.assertEqual(kept["schema_differences"], ["Person"])
		self.assertEqual(kept["existing_keys"], {})

	def test_application_versions_that_differ_are_refused(self):
		record = self.refused(*self.sealed(versions={"ucm": "0.0.0-not-this-site"}))
		self.assertEqual(record.finding, "Preflight refused: application versions differ")
		kept = json.loads(record.reconciliation_json)["preflight"]
		self.assertEqual(kept["release_differences"]["ucm"]["published"], "0.0.0-not-this-site")
		self.assertEqual(kept["schema_differences"], [])

	def test_record_ids_already_on_the_site_are_refused(self):
		from ucm.tests.utils import make_context, make_person

		person = make_person(make_context("LDX1")).name
		record = self.refused(*self.sealed(persons=[person, "a-person-this-site-does-not-hold"]))
		self.assertEqual(record.finding, "Preflight refused: record IDs are already on this site")
		kept = json.loads(record.reconciliation_json)["preflight"]
		# a count by table, never the IDs themselves
		self.assertEqual(kept["existing_keys"], {"Person": 1})
		self.assertNotIn(person, record.reconciliation_json)

	def test_every_reason_is_named(self):
		schema = site_schema()
		schema["Case"] += "\n  `case_colour` varchar(140)"
		record = self.refused(*self.sealed(schema=schema, versions={"ucm": "0.0.0-not-this-site"}))
		self.assertEqual(
			record.finding, "Preflight refused: tables differ from this site, application versions differ"
		)

	def test_a_refused_run_keeps_no_file_and_no_route(self):
		path, run = self.sealed(versions={"ucm": "0.0.0-not-this-site"})
		record = self.refused(path, run)
		self.assertFalse(publication_receiver.local_publication(run).exists())
		self.assertEqual((record.applied, record.received, record.document_count), (0, 1, 1))
		self.assertIsNone(record.write_plan_json)
		self.assertIsNone(record.write_route)
		self.assertIsNone(record.publication_stamp)
		self.assertTrue(record.display_id.startswith("MIG-"))
		self.assertEqual(frappe.db.count(api.BATCH, {"migration_run": run}), 0)

	def test_the_form_shows_one_failed_step_with_what_differs(self):
		path, run = self.sealed(versions={"ucm": "0.0.0-not-this-site"})
		self.refused(path, run)
		shown = overview(run)
		self.assertEqual([(step["step"], step["status"]) for step in shown["steps"]], [("stage", "failed")])
		self.assertIsNotNone(shown["steps"][0]["seconds"])
		self.assertIn("ucm", shown["steps"][0]["facts"]["release_differences"])
		self.assertIsNone(shown["facts"]["route"])
		self.assertEqual(shown["findings"], [])
		self.assertTrue(all(row["stored"] is None for row in shown["records"]))

	def test_a_refused_run_cannot_be_applied_or_activated(self):
		path, run = self.sealed(versions={"ucm": "0.0.0-not-this-site"})
		self.refused(path, run)
		for action in (api.apply, api.activate, api.pause):
			self.assertRaisesRegex(ValueError, "Cannot .* a run in Rejected", action, run)

	def test_a_refused_delegation_can_be_published_again(self):
		"""A refusal does not hold the context: another file of the same delegation may be staged."""
		path, run = self.sealed(versions={"ucm": "0.0.0-not-this-site"})
		self.refused(path, run)
		with tempfile.TemporaryDirectory() as second:
			other_path, other = seal(second, self.context)
			self.addCleanup(remove_files, other)
			result = publication_receiver.stage(str(other_path), other)
		self.assertEqual(result["state"], "Publication staged")
		self.assertEqual(frappe.db.get_value(api.RUN, run, "state"), "Rejected")

	def test_the_same_file_is_checked_again_and_takes_over_the_refusal(self):
		"""The site was behind the publication; once it is brought in line, the same file is staged, under the
		run and the number the refusal had."""
		path, run = self.sealed()
		behind = {"ucm": {"published": "16.1.0", "target": "16.0.0"}}
		with patch.object(publication_receiver, "release_differences", return_value=behind):
			number = self.refused(path, run).display_id
		result = publication_receiver.stage(path, run)
		record = frappe.get_doc(api.RUN, run)
		self.assertEqual((result["run"], record.state), (run, "Publication staged"))
		self.assertEqual(record.display_id, number)
		self.assertIsNone(record.finding)
		self.assertIsNone(record.reconciliation_json)
		self.assertTrue(publication_receiver.local_publication(run).exists())
		self.assertEqual(record.write_route, "Document API")
		steps = [(step["step"], step["status"]) for step in overview(run)["steps"]]
		self.assertEqual(steps[:2], [("stage", "done"), ("write", "waiting")])


class TestStaged(StagingCase):
	def test_a_fitting_publication_is_staged_with_its_list_columns(self):
		path, run = self.sealed()
		result = publication_receiver.stage(path, run)
		record = frappe.get_doc(api.RUN, run)
		self.assertEqual(result["state"], "Publication staged")
		code = frappe.db.get_value("Context", self.context, "context_code")
		self.assertEqual(record.delegation, f"Test {code[:-1] if code.endswith('0') else code} ({code})")
		self.assertEqual(record.write_route, "Document API")
		timeline = json.loads(record.timeline_json)
		self.assertEqual(timeline["stage"]["business_batches"], 1)
		self.assertGreater(timeline["stage"]["seconds"], 0)

	def test_staging_the_same_file_twice_returns_the_run(self):
		path, run = self.sealed()
		publication_receiver.stage(path, run)
		modified = frappe.db.get_value(api.RUN, run, "modified")
		self.assertEqual(publication_receiver.stage(path, run)["state"], "Publication staged")
		self.assertEqual(frappe.db.get_value(api.RUN, run, "modified"), modified)

	def test_the_site_setting_chooses_the_route_of_a_staged_run(self):
		path, run = self.sealed()
		with patch.dict(frappe.conf, {"ucm_migration_route": "Set-based SQL"}):
			publication_receiver.stage(path, run)
		self.assertEqual(frappe.db.get_value(api.RUN, run, "write_route"), "Set-based SQL")
		steps = [step["step"] for step in overview(run)["steps"]]
		self.assertEqual(steps, ["stage", "write", "derive", "verify", "activate"])

	def test_choosing_a_route_updates_the_list_column(self):
		from ucm_migration_loader import write_routes

		path, run = self.sealed()
		publication_receiver.stage(path, run)
		write_routes.select(run, "Set-based SQL")
		self.assertEqual(frappe.db.get_value(api.RUN, run, "write_route"), "Set-based SQL")


class TestTransfer(StagingCase):
	def send(self, path, run, publisher):
		"""Send a sealed file as the push command does: approved by an operator, uploaded by the publisher."""
		data = open(path, "rb").read()
		publication_transport.approve(run, publisher, hashlib.sha256(data).hexdigest(), len(data))
		frappe.set_user(publisher)
		for offset in range(0, len(data), publication_transport.CHUNK):
			chunk = data[offset : offset + publication_transport.CHUNK]
			publication_transport.upload(run, offset, base64.b64encode(chunk).decode())
		frappe.set_user("Administrator")

	def test_the_sending_account_reads_the_run_it_sent_and_no_other(self):
		publisher, other = make_account("sender"), make_account("other-sender")
		path, run = self.sealed()
		self.send(path, run, publisher)
		result = publication_transport.receive(run)
		self.assertEqual(result["state"], "Publication staged")
		self.assertEqual(frappe.db.get_value(api.RUN, run, "publishing_user"), publisher)

		frappe.set_user(publisher)
		self.assertEqual(api.status(run)["state"], "Publication staged")
		self.assertEqual(api.report(run)["run"], run)
		# it may ask for the metrics: there are none before the verification
		self.assertRaisesRegex(ValueError, "no verification report", api.quality, run)
		# and nothing else: the loader's commands and the run's form stay with the operators
		for command in (api.apply, api.activate, api.discard, api.pause, overview):
			self.assertRaises(frappe.PermissionError, command, run)
		self.assertFalse(frappe.has_permission(api.RUN, "read", run))

		frappe.set_user(other)
		self.assertRaises(frappe.PermissionError, api.status, run)
		self.assertRaises(frappe.PermissionError, api.quality, run)

	def test_naming_the_sender_does_not_change_the_runs_time(self):
		"""The run's metrics carry the time of its last step, which naming the sender is not."""
		publisher = make_account("sender")
		path, run = self.sealed()
		self.send(path, run, publisher)
		with patch.object(run_overview, "staged", wraps=run_overview.staged) as staged:
			publication_transport.receive(run)
		staged.assert_called_once()
		record = frappe.get_doc(api.RUN, run)
		self.assertEqual(record.modified, record.creation)

	def test_a_refused_transfer_names_no_sender(self):
		publisher = make_account("sender")
		path, run = self.sealed(versions={"ucm": "0.0.0-not-this-site"})
		self.send(path, run, publisher)
		with patch.object(frappe.db, "commit"):
			self.assertRaisesRegex(
				ValueError, "Publication preflight failed", publication_transport.receive, run
			)
		self.assertEqual(frappe.db.get_value(api.RUN, run, "state"), "Rejected")
		self.assertIsNone(frappe.db.get_value(api.RUN, run, "publishing_user"))
		frappe.set_user(publisher)
		self.assertRaises(frappe.PermissionError, api.status, run)

	def test_only_the_approved_account_can_send(self):
		publisher, other = make_account("sender"), make_account("other-sender")
		path, run = self.sealed()
		data = open(path, "rb").read()
		publication_transport.approve(run, publisher, hashlib.sha256(data).hexdigest(), len(data))
		frappe.set_user(other)
		self.assertRaises(frappe.PermissionError, publication_transport.status, run)
		self.assertRaises(
			frappe.PermissionError, publication_transport.upload, run, 0, base64.b64encode(data).decode()
		)
