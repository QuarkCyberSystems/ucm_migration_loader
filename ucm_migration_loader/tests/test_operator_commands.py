"""The site operator's two commands: approve a hand-off for one publishing account, and stage the file once it
has arrived. Each outcome is one the operator is told in a sentence."""

import base64
import json
import tempfile
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ucm_migration_loader import api, operator, publication_transport
from ucm_migration_loader.commands import said
from ucm_migration_loader.tests.utils import hand_off, hidden_context, make_account, remove_files, seal


class TestOperatorCommands(IntegrationTestCase):
	codes = iter(range(1000))

	def setUp(self):
		super().setUp()
		self.directory = self.enterContext(tempfile.TemporaryDirectory())
		self.context = hidden_context(f"LDO{next(self.codes)}")
		self.publisher = make_account("sender")

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def sealed(self, **changes):
		path, run = seal(self.directory, self.context, **changes)
		self.addCleanup(remove_files, run)
		return path, run, str(hand_off(path, run))

	def send(self, path, run):
		data = path.read_bytes()
		frappe.set_user(self.publisher)
		publication_transport.upload(run, 0, base64.b64encode(data).decode())
		frappe.set_user("Administrator")

	def test_an_approval_says_what_was_approved(self):
		path, run, handoff = self.sealed()
		result = operator.approve_handoff(handoff, self.publisher)
		self.assertEqual(
			(result["run"], result["publisher"], result["rows"], result["batches"], result["bytes"]),
			(run, self.publisher, 1, 1, path.stat().st_size),
		)
		text, code = said(result, handoff)
		self.assertEqual(code, 0)
		self.assertIn(f"Approved run {run[:12]}… for {self.publisher}: 1 rows in 1 batches", text)
		self.assertIn(f"ucm-migration stage {handoff}", text)

	def test_a_hand_off_that_was_changed_is_not_approved(self):
		_path, _run, handoff = self.sealed()
		changed = json.loads(open(handoff).read())
		changed["counts"]["Person"] = 2
		with open(handoff, "w") as file:
			file.write(json.dumps(changed))
		self.assertRaisesRegex(
			ValueError, "does not match", operator.approve_handoff, handoff, self.publisher
		)

	def test_an_approval_needs_a_publishing_account(self):
		_path, _run, handoff = self.sealed()
		self.assertRaisesRegex(
			ValueError, "enabled publishing account", operator.approve_handoff, handoff, "Administrator"
		)

	def test_a_run_never_approved_here_is_said_so(self):
		_path, run, handoff = self.sealed()
		result = operator.stage_arrived(handoff)
		self.assertEqual(result, {"outcome": "not approved", "run": run})
		text, code = said(result, handoff)
		self.assertEqual(code, 1)
		self.assertIn("was not approved on this site", text)

	def test_a_file_that_has_not_arrived_is_not_staged(self):
		path, run, handoff = self.sealed()
		operator.approve_handoff(handoff, self.publisher)
		result = operator.stage_arrived(handoff)
		self.assertEqual(
			(result["outcome"], result["arrived"], result["bytes"]), ("incomplete", 0, path.stat().st_size)
		)
		self.assertFalse(frappe.db.exists(api.RUN, run))
		text, code = said(result, handoff)
		self.assertEqual(code, 1)
		self.assertIn("has not arrived completely", text)
		self.assertIn("ucm-push send again", text)

	def test_an_arrived_file_is_staged_by_its_hand_off_or_its_run(self):
		path, run, handoff = self.sealed()
		operator.approve_handoff(handoff, self.publisher)
		self.send(path, run)
		result = operator.stage_arrived(handoff)
		self.assertEqual((result["outcome"], result["state"]), ("staged", "Publication staged"))
		self.assertEqual((result["rows"], result["batches"]), (1, 1))
		self.assertEqual(frappe.db.get_value(api.RUN, run, "publishing_user"), self.publisher)
		text, code = said(result, handoff)
		self.assertEqual(code, 0)
		self.assertIn(f"Staged as {result['display_id']}: 1 rows in 1 batches, nothing written yet", text)
		# asked again, by the run's identifier: the same run
		self.assertEqual(operator.stage_arrived(run)["display_id"], result["display_id"])

	def test_a_file_the_site_refuses_is_kept_as_a_refused_run(self):
		path, run, handoff = self.sealed(versions={"ucm": "0.0.0-not-this-site"})
		operator.approve_handoff(handoff, self.publisher)
		self.send(path, run)
		# the refusal is committed by the receiver; the test keeps it in its own transaction
		with patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"):
			result = operator.stage_arrived(handoff)
		self.assertEqual(result["outcome"], "refused")
		self.assertEqual(result["finding"], "Preflight refused: application versions differ")
		text, code = said(result, handoff)
		self.assertEqual(code, 1)
		self.assertIn(f"Refused, and kept as {result['display_id']}", text)

	def test_only_an_operator_stages(self):
		_path, _run, handoff = self.sealed()
		frappe.set_user(self.publisher)
		self.assertRaises(frappe.PermissionError, operator.stage_arrived, handoff)
		self.assertRaises(frappe.PermissionError, operator.approve_handoff, handoff, self.publisher)
