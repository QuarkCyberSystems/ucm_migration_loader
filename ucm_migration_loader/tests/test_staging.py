"""A sealed publication arriving on the site: staged when it fits, and kept as a refused run, with the reason and
without its file, when it does not. A file sent over HTTPS leaves its run readable by the account that sent it."""

import base64
import errno
import hashlib
import json
import shutil
import tempfile
import uuid
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ucm_migration_loader import (
	api,
	constants,
	publication_index,
	publication_receiver,
	publication_transport,
	publication_worker,
)
from ucm_migration_loader.tests.utils import (
	hidden_context,
	make_account,
	new_run_name,
	remove_files,
	seal,
	site_schema,
)
from ucm_migration_loader.ucm_migration_loader import run_overview, set_based
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
		return frappe.get_doc(constants.RUN, run)


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
		self.assertEqual(frappe.db.count(constants.BATCH, {"migration_run": run}), 0)

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
			self.assertRaisesRegex(frappe.ValidationError, "Cannot .* a run in Rejected", action, run)

	def test_a_refused_delegation_can_be_published_again(self):
		"""A refusal does not hold the context: another file of the same delegation may be staged."""
		path, run = self.sealed(versions={"ucm": "0.0.0-not-this-site"})
		self.refused(path, run)
		with tempfile.TemporaryDirectory() as second:
			other_path, other = seal(second, self.context)
			self.addCleanup(remove_files, other)
			result = publication_receiver.stage(str(other_path), other)
		self.assertEqual(result["state"], "Publication staged")
		self.assertEqual(frappe.db.get_value(constants.RUN, run, "state"), "Rejected")

	def test_the_same_file_is_checked_again_and_takes_over_the_refusal(self):
		"""The site was behind the publication; once it is brought in line, the same file is staged, under the
		run and the number the refusal had."""
		path, run = self.sealed()
		behind = {"ucm": {"published": "16.1.0", "target": "16.0.0"}}
		with patch.object(publication_receiver, "release_differences", return_value=behind):
			number = self.refused(path, run).display_id
		result = publication_receiver.stage(path, run)
		record = frappe.get_doc(constants.RUN, run)
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
		record = frappe.get_doc(constants.RUN, run)
		self.assertEqual(result["state"], "Publication staged")
		name, code = frappe.db.get_value("Context", self.context, ["context_name", "context_code"])
		self.assertEqual(record.delegation, f"{name} ({code})")
		self.assertEqual(record.write_route, "Document API")
		timeline = json.loads(record.timeline_json)
		self.assertEqual(timeline["stage"]["business_batches"], 1)
		self.assertGreater(timeline["stage"]["seconds"], 0)

	def test_staging_the_same_file_twice_returns_the_run(self):
		path, run = self.sealed()
		publication_receiver.stage(path, run)
		modified = frappe.db.get_value(constants.RUN, run, "modified")
		self.assertEqual(publication_receiver.stage(path, run)["state"], "Publication staged")
		self.assertEqual(frappe.db.get_value(constants.RUN, run, "modified"), modified)

	def test_a_delegation_another_run_holds_is_not_staged_twice(self):
		"""The staging refuses in the words the approval's dialog shows: both ask the same check."""
		path, run = self.sealed()
		held = publication_receiver.stage(path, run)
		self.assertEqual(held["state"], "Publication staged")
		number = frappe.db.get_value(constants.RUN, run, "display_id")
		other_path, other = seal(
			tempfile.mkdtemp(dir=self.directory), self.context, persons=[str(uuid.uuid4())]
		)
		self.addCleanup(remove_files, other)
		other_path = str(other_path)
		self.assertEqual(
			publication_receiver.context_state(self.context, other)["problem"],
			f"Run {number} already holds this delegation",
		)
		self.assertRaisesRegex(
			ValueError,
			f"Run {number} already holds this delegation",
			publication_receiver.stage,
			other_path,
			other,
		)
		self.assertFalse(frappe.db.exists(constants.RUN, other))
		# asked for the run that holds it, the same context is no obstacle
		self.assertIsNone(publication_receiver.context_state(self.context, run)["problem"])

	def test_the_site_setting_chooses_the_route_of_a_staged_run(self):
		path, run = self.sealed()
		with patch.dict(frappe.conf, {"ucm_migration_route": "Set-based SQL"}):
			publication_receiver.stage(path, run)
		self.assertEqual(frappe.db.get_value(constants.RUN, run, "write_route"), "Set-based SQL")
		steps = [step["step"] for step in overview(run)["steps"]]
		self.assertEqual(steps, ["stage", "write", "derive", "verify", "activate"])

	def test_choosing_a_route_updates_the_list_column(self):
		from ucm_migration_loader import write_routes

		path, run = self.sealed()
		publication_receiver.stage(path, run)
		write_routes.select(run, "Set-based SQL")
		self.assertEqual(frappe.db.get_value(constants.RUN, run, "write_route"), "Set-based SQL")


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
		self.assertEqual(frappe.db.get_value(constants.RUN, run, "publishing_user"), publisher)

		frappe.set_user(publisher)
		self.assertEqual(api.status(run)["state"], "Publication staged")
		self.assertEqual(api.report(run)["run"], run)
		# it may ask for the metrics: there are none before the verification
		self.assertRaisesRegex(frappe.ValidationError, "no verification report", api.quality, run)
		# and nothing else: the loader's commands and the run's form stay with the operators
		for command in (api.apply, api.activate, api.discard, api.pause, overview):
			self.assertRaises(frappe.PermissionError, command, run)
		self.assertFalse(frappe.has_permission(constants.RUN, "read", run))

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
		record = frappe.get_doc(constants.RUN, run)
		self.assertEqual(record.modified, record.creation)

	def test_a_refused_transfer_names_no_sender(self):
		publisher = make_account("sender")
		path, run = self.sealed(versions={"ucm": "0.0.0-not-this-site"})
		self.send(path, run, publisher)
		with patch.object(frappe.db, "commit"):
			self.assertRaisesRegex(
				ValueError, "Publication preflight failed", publication_transport.receive, run
			)
		self.assertEqual(frappe.db.get_value(constants.RUN, run, "state"), "Rejected")
		self.assertIsNone(frappe.db.get_value(constants.RUN, run, "publishing_user"))
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


class TestArrival(StagingCase):
	"""A sealed file arrives in pieces into one file, and the site notes how far it is safely on the disk."""

	PIECE = 1024

	def setUp(self):
		super().setUp()
		# pieces of a kilobyte, so that a file of the tests has several
		self.enterContext(patch.object(publication_transport, "CHUNK", self.PIECE))
		self.publisher = make_account("sender")
		self.path, self.run = self.sealed()
		self.data = open(self.path, "rb").read()
		self.assertGreater(len(self.data), 3 * self.PIECE)
		publication_transport.approve(
			self.run, self.publisher, hashlib.sha256(self.data).hexdigest(), len(self.data)
		)
		frappe.set_user(self.publisher)

	def piece(self, number: int, data: bytes | None = None) -> dict:
		start = number * self.PIECE
		chunk = (data or self.data)[start : start + self.PIECE]
		return publication_transport.upload(self.run, start, base64.b64encode(chunk).decode())

	def arrived(self) -> bytes:
		return publication_receiver.arriving(self.run).read_bytes()

	def test_the_pieces_make_one_file_and_no_other(self):
		self.assertEqual(self.piece(0)["offset"], self.PIECE)
		self.assertEqual(self.piece(1)["offset"], 2 * self.PIECE)
		self.assertEqual(self.arrived(), self.data[: 2 * self.PIECE])
		self.assertEqual(publication_transport.status(self.run)["offset"], 2 * self.PIECE)
		inbox = publication_transport.folder(self.run)
		self.assertEqual(sorted(kept.name for kept in inbox.iterdir()), ["approval.json", "offset"])
		self.assertEqual(publication_receiver.arriving(self.run).stat().st_mode & 0o777, 0o600)

	def test_a_piece_out_of_turn_is_refused(self):
		self.piece(0)
		self.assertRaisesRegex(ValueError, "Unexpected publication offset", self.piece, 2)
		self.assertEqual(self.arrived(), self.data[: self.PIECE])

	def test_a_piece_sent_twice_is_accepted_when_it_is_the_same(self):
		self.piece(0)
		self.piece(1)
		self.assertEqual(self.piece(0)["offset"], 2 * self.PIECE)
		self.assertRaisesRegex(ValueError, "Replayed chunk differs", self.piece, 0, bytes(len(self.data)))
		self.assertEqual(self.arrived(), self.data[: 2 * self.PIECE])

	def test_a_piece_cut_short_by_a_crash_is_sent_again(self):
		"""What the file holds beyond the noted end was never acknowledged: the sender is told the noted end,
		and the next piece replaces it."""
		self.piece(0)
		with publication_receiver.arriving(self.run).open("ab") as file:
			file.write(b"half a piece that was never noted")
		self.assertEqual(publication_transport.status(self.run)["offset"], self.PIECE)
		self.piece(1)
		self.assertEqual(self.arrived(), self.data[: 2 * self.PIECE])

	def test_a_full_disk_is_said_in_a_sentence_and_the_transfer_goes_on_later(self):
		self.piece(0)
		full = OSError(errno.ENOSPC, "No space left on device")
		with patch.object(publication_transport.os, "pwrite", side_effect=full):
			self.assertRaisesRegex(ValueError, "The site's disk is full", self.piece, 1)
		self.assertEqual(publication_transport.status(self.run)["offset"], self.PIECE)
		self.piece(1)
		self.assertEqual(self.arrived(), self.data[: 2 * self.PIECE])

	def test_the_whole_file_is_staged_where_it_arrived(self):
		for number in range(-(-len(self.data) // self.PIECE)):
			arrived = self.piece(number)
		self.assertEqual(arrived["offset"], len(self.data))
		self.assertEqual(self.arrived(), self.data)
		frappe.set_user("Administrator")
		self.assertEqual(publication_transport.receive(self.run)["state"], "Publication staged")
		self.assertFalse(publication_receiver.arriving(self.run).exists())
		self.assertEqual(publication_receiver.local_publication(self.run).read_bytes(), self.data)

	def test_a_file_that_differs_from_the_approved_one_is_not_staged(self):
		changed = bytearray(self.data)
		changed[self.PIECE + 5] ^= 1
		for number in range(-(-len(self.data) // self.PIECE)):
			self.piece(number, bytes(changed))
		frappe.set_user("Administrator")
		self.assertRaisesRegex(
			ValueError, "Transfer differs from the approved file", publication_transport.receive, self.run
		)
		self.assertFalse(publication_receiver.local_publication(self.run).exists())

	def test_a_withdrawn_approval_removes_what_arrived(self):
		self.piece(0)
		frappe.set_user("Administrator")
		publication_receiver.remove_arrived(self.run)
		self.assertTrue(publication_receiver.arriving(self.run).exists())  # not before the commit
		frappe.db.after_commit.run()
		self.assertFalse(publication_receiver.arriving(self.run).exists())
		self.assertFalse(publication_transport.folder(self.run).exists())


class TestSpace(StagingCase):
	"""An approval is refused where the site's disk cannot hold the file."""

	def approve(self, free: int, size: int):
		run = new_run_name()
		self.addCleanup(remove_files, run)
		usage = shutil.disk_usage(publication_receiver.local_publication(run).parent)
		with patch.object(shutil, "disk_usage", return_value=usage._replace(free=free)):
			return run, publication_transport.approve(run, make_account("sender"), "a" * 64, size)

	def test_a_file_the_disk_cannot_hold_is_not_approved(self):
		gib = 1024**3
		with self.assertRaisesRegex(
			frappe.ValidationError, "has 10,737 MB free, and this file needs 64,492 MB"
		):
			self.approve(free=10 * gib, size=60 * gib)

	def test_a_file_the_disk_can_hold_is_approved(self):
		gib = 1024**3
		run, approval = self.approve(free=61 * gib, size=60 * gib)
		self.assertEqual(approval["size"], 60 * gib)
		self.assertTrue((publication_transport.folder(run) / "approval.json").is_file())

	def test_the_file_needs_no_more_than_its_own_size_and_a_reserve(self):
		gib = 1024**3
		with self.assertRaises(frappe.ValidationError):
			self.approve(free=60 * gib + publication_transport.RESERVE - 1, size=60 * gib)
		self.approve(free=60 * gib + publication_transport.RESERVE, size=60 * gib)


class TestActivatedRun(StagingCase):
	"""An activated run's file leaves the site with its activation, and the run answers without it."""

	def staged(self):
		path, run = self.sealed()
		publication_receiver.stage(path, run)
		return path, run

	def test_the_file_and_its_index_leave_once_the_activation_is_committed(self):
		_path, run = self.staged()
		dest = publication_receiver.local_publication(run)
		index = publication_index.index_path(dest)
		self.assertTrue(dest.exists() and index.exists())
		publication_receiver.remove_staged_file(run)
		# not before the commit: an activation that is rolled back keeps its file
		self.assertTrue(dest.exists() and index.exists())
		frappe.db.after_commit.run()
		self.assertFalse(dest.exists() or index.exists())

	def test_an_activated_run_answers_without_its_file(self):
		path, run = self.staged()
		frappe.db.set_value(constants.RUN, run, "state", constants.ACTIVATED)
		remove_files(run)
		self.assertEqual(publication_worker.activate(run)["state"], "Activated")
		self.assertEqual(set_based.activate(run)["state"], "Activated")
		# the same file, sent or staged once more, is not kept again
		self.assertEqual(publication_receiver.stage(path, run)["state"], "Activated")
		self.assertFalse(publication_receiver.local_publication(run).exists())

	def test_a_step_that_reads_the_file_says_that_it_is_gone(self):
		_path, run = self.staged()
		remove_files(run)
		self.assertRaisesRegex(
			ValueError, "file is no longer on this site", publication_receiver.rebuild_index, run
		)
