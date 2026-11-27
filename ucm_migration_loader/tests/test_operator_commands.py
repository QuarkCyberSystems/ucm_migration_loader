"""The site operator's part, as the Desk and the bench commands do it: a Migration Admin approves a hand-off for
one publishing account, the publication becomes a run that waits for its file, and the file is staged when all
of it has arrived."""

import base64
import json
import tempfile
import uuid
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ucm_migration_loader import api, constants, operator, publication_transport
from ucm_migration_loader.commands import said
from ucm_migration_loader.tests.utils import hand_off, hidden_context, make_account, remove_files, seal
from ucm_migration_loader.ucm_migration_loader import run_overview
from ucm_migration_loader.ucm_migration_loader.doctype.ucm_migration_run import ucm_migration_run as desk

AWAITING = "Awaiting publication"


class OperatorCase(IntegrationTestCase):
	codes = iter(range(1000))

	def setUp(self):
		super().setUp()
		self.directory = self.enterContext(tempfile.TemporaryDirectory())
		self.context = hidden_context(f"LDO{next(self.codes)}")
		self.publisher = make_account("sender")
		self.admin = make_account("migration-admin", operator=True)
		frappe.set_user(self.admin)

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def sealed(self, context=None, **changes):
		path, run = seal(tempfile.mkdtemp(dir=self.directory), context or self.context, **changes)
		self.addCleanup(remove_files, run)
		return path, run, json.loads(hand_off(path, run).read_text())

	def send(self, path, run, publisher=None):
		"""Send the whole file as the publishing account does. The job its last piece queues is not run here."""
		acting = frappe.session.user
		frappe.set_user(publisher or self.publisher)
		publication_transport.upload(run, 0, base64.b64encode(path.read_bytes()).decode())
		frappe.set_user(acting)

	def state(self, run):
		return frappe.db.get_value(constants.RUN, run, "state")


class TestApproval(OperatorCase):
	def test_a_hand_off_is_read_before_it_is_approved(self):
		path, run, handoff = self.sealed()
		found = operator.inspect_handoff(handoff)
		self.assertEqual((found["run"], found["rows"], found["batches"]), (run, 1, 1))
		self.assertEqual((found["bytes"], found["snapshot"]), (path.stat().st_size, "TEST-1"))
		self.assertEqual(found["counts"], {"Person": 1})
		self.assertEqual(found["context"]["id"], self.context)
		self.assertTrue(found["context"]["exists"])
		self.assertIsNone(found["context"]["problem"])
		self.assertIsNone(found["known"])
		self.assertFalse(frappe.db.exists(constants.RUN, run))  # reading it changes nothing

	def test_an_approval_makes_a_run_that_waits_for_its_file(self):
		path, run, handoff = self.sealed()
		result = operator.approve(handoff, self.publisher)
		record = frappe.get_doc(constants.RUN, run)
		self.assertEqual((record.state, record.publishing_user), (AWAITING, self.publisher))
		self.assertEqual((record.document_count, record.received, record.applied), (1, 1, 0))
		self.assertEqual(result["display_id"], record.display_id)
		approval = json.loads(record.approval_json)
		self.assertEqual((approval["by"], approval["publisher"]), (self.admin, self.publisher))
		self.assertEqual(approval["size"], path.stat().st_size)
		self.assertFalse(result["context_made"])
		text, code = said(result, "handoff.json")
		self.assertEqual(code, 0)
		self.assertIn(f"It waits for its file as {record.display_id}", text)

	def test_the_waiting_run_shows_who_approved_it_and_what_has_arrived(self):
		path, run, handoff = self.sealed()
		operator.approve(handoff, self.publisher)
		shown = run_overview.build(frappe.get_doc(constants.RUN, run))
		statuses = [(step["step"], step["status"]) for step in shown["steps"]]
		self.assertEqual(statuses[0], ("stage", "waiting"))
		self.assertTrue(all(status == "waiting" for _step, status in statuses[1:]))
		facts = shown["steps"][0]["facts"]
		self.assertEqual((facts["by"], facts["publisher"]), (self.admin, self.publisher))
		self.assertEqual((facts["arrived"], facts["bytes"]), (0, path.stat().st_size))
		self.assertIsNone(shown["facts"]["route"])
		self.send(path, run)
		arrived = run_overview.build(frappe.get_doc(constants.RUN, run))["steps"][0]
		self.assertEqual((arrived["status"], arrived["facts"]["arrived"]), ("running", path.stat().st_size))

	def test_the_sender_reads_the_waiting_run(self):
		_path, run, handoff = self.sealed()
		operator.approve(handoff, self.publisher)
		frappe.set_user(self.publisher)
		self.assertEqual(api.status(run)["state"], AWAITING)

	def test_a_hand_off_that_was_changed_is_not_approved(self):
		_path, _run, handoff = self.sealed()
		handoff["counts"]["Person"] = 2
		self.assertRaisesRegex(
			frappe.ValidationError, "does not match", operator.approve, handoff, self.publisher
		)
		self.assertRaisesRegex(frappe.ValidationError, "does not match", operator.inspect_handoff, handoff)

	def test_the_sender_and_the_approver_are_two_accounts(self):
		_path, run, handoff = self.sealed()
		self.assertRaisesRegex(
			frappe.ValidationError, "cannot be the one that approves", operator.approve, handoff, self.admin
		)
		other = make_account("second-admin", operator=True)
		self.assertRaisesRegex(
			frappe.ValidationError, "cannot be the publishing account", operator.approve, handoff, other
		)
		self.assertRaisesRegex(
			frappe.ValidationError, "enabled publishing account", operator.approve, handoff, "Guest"
		)
		self.assertFalse(frappe.db.exists(constants.RUN, run))

	def test_only_a_migration_admin_approves(self):
		_path, _run, handoff = self.sealed()
		manager = make_account("site-manager")
		frappe.set_user("Administrator")
		frappe.get_doc("User", manager).add_roles("System Manager")
		for account in (self.publisher, manager):
			frappe.set_user(account)
			self.assertRaises(frappe.PermissionError, operator.inspect_handoff, handoff)
			self.assertRaises(frappe.PermissionError, operator.approve, handoff, self.publisher)
			self.assertRaises(frappe.PermissionError, operator.stage_arrived, handoff["run"])
		# a System Manager reads the runs, and gives the role; it does not act on a run
		self.assertTrue(frappe.has_permission(constants.RUN, "read"))
		self.assertRaises(frappe.PermissionError, api.activate, handoff["run"])

	def test_an_approval_can_be_repeated_but_not_changed(self):
		_path, run, handoff = self.sealed()
		first = operator.approve(handoff, self.publisher)
		self.assertEqual(operator.approve(handoff, self.publisher)["display_id"], first["display_id"])
		other = make_account("other-sender")
		self.assertRaisesRegex(frappe.ValidationError, "approved before", operator.approve, handoff, other)
		self.assertEqual(frappe.db.get_value(constants.RUN, run, "publishing_user"), self.publisher)

	def test_an_approval_can_be_withdrawn(self):
		path, run, handoff = self.sealed()
		operator.approve(handoff, self.publisher)
		self.send(path, run)
		self.assertEqual(api.discard(run)["state"], "Discarded")
		self.assertEqual(self.state(run), "Discarded")
		# the delegation's context is free for another publication
		_other_path, other, other_handoff = self.sealed(persons=[str(uuid.uuid4())])
		self.assertEqual(self.state(operator.approve(other_handoff, self.publisher)["run"]), AWAITING)
		self.assertNotEqual(other, run)


class TestProposal(OperatorCase):
	"""The publishing account proposes its hand-off through ucm-push; a Migration Admin approves it in the Desk
	with the check code the publishing operator gave them."""

	def setUp(self):
		super().setUp()
		self.proposer = make_account("proposer", publisher=True)

	def proposed(self, context=None, **changes):
		path, run, handoff = self.sealed(context=context, **changes)
		frappe.set_user(self.proposer)
		result = api.propose(handoff)
		frappe.set_user(self.admin)
		return path, run, handoff, result

	def code(self, handoff):
		from ucm_migration_loader.contract import check_code

		return check_code(handoff["file_sha256"])

	def test_a_proposal_is_a_run_that_waits_for_approval(self):
		path, run, _handoff, result = self.proposed()
		record = frappe.get_doc(constants.RUN, run)
		self.assertEqual((record.state, record.publishing_user), ("Proposed", self.proposer))
		self.assertEqual((result["state"], result["display_id"]), ("Proposed", record.display_id))
		self.assertEqual(record.document_count, 1)
		# nothing is accepted for it yet: no approval, so no transfer
		frappe.set_user(self.proposer)
		self.assertRaises(frappe.PermissionError, publication_transport.status, run)
		self.assertEqual(api.status(run)["state"], "Proposed")
		frappe.set_user(self.admin)
		shown = run_overview.build(record)
		self.assertEqual((shown["steps"][0]["step"], shown["steps"][0]["status"]), ("stage", "waiting"))
		self.assertEqual(shown["steps"][0]["facts"]["proposed_by"], self.proposer)
		self.assertEqual(shown["steps"][0]["facts"]["bytes"], path.stat().st_size)
		people = shown["facts"]
		self.assertEqual(people["proposed_by"], self.proposer)
		self.assertEqual((people["approved_by"], people["staged_by"]), (None, None))

	def test_only_a_publishing_account_proposes(self):
		_path, run, handoff = self.sealed()
		for account in (self.admin, self.publisher, "Guest"):
			frappe.set_user(account)
			self.assertRaises(frappe.PermissionError, api.propose, handoff)
		self.assertFalse(frappe.db.exists(constants.RUN, run))

	def test_a_hand_off_that_was_changed_or_is_for_another_site_is_not_proposed(self):
		_path, run, handoff = self.sealed()
		frappe.set_user(self.proposer)
		self.assertRaisesRegex(
			frappe.ValidationError, "does not match", api.propose, {**handoff, "site": "another.example"}
		)
		handoff["counts"]["Person"] = 5
		self.assertRaisesRegex(frappe.ValidationError, "does not match", api.propose, handoff)
		self.assertFalse(frappe.db.exists(constants.RUN, run))

	def test_a_proposal_can_be_repeated_by_its_account_only(self):
		_path, run, handoff, result = self.proposed()
		frappe.set_user(self.proposer)
		self.assertEqual(api.propose(handoff), result)
		frappe.set_user(make_account("other-proposer", publisher=True))
		self.assertRaises(frappe.PermissionError, api.propose, handoff)
		self.assertEqual(frappe.db.get_value(constants.RUN, run, "publishing_user"), self.proposer)

	def test_the_approver_sees_the_proposal_without_its_fingerprint(self):
		path, run, handoff, _result = self.proposed()
		found = desk.inspect_proposed(run)
		self.assertEqual((found["rows"], found["bytes"]), (1, path.stat().st_size))
		self.assertEqual(found["proposed_by"], self.proposer)
		self.assertTrue(found["context"]["exists"])
		self.assertNotIn(handoff["file_sha256"], json.dumps(found))
		self.assertNotIn(self.code(handoff).replace("-", "").lower(), json.dumps(found).lower())

	def test_the_check_code_approves_and_a_wrong_one_does_not(self):
		_path, run, handoff, _result = self.proposed()
		self.assertRaisesRegex(
			frappe.ValidationError, "check code does not match", desk.approve_proposed, run, "0000-0000"
		)
		self.assertRaisesRegex(
			frappe.ValidationError, "check code does not match", desk.approve_proposed, run, ""
		)
		self.assertEqual(self.state(run), "Proposed")
		typed = self.code(handoff).lower().replace("-", " ")  # as a person types what they were told
		approved = desk.approve_proposed(run, typed)
		self.assertEqual((approved["run"], self.state(run)), (run, AWAITING))
		facts = json.loads(frappe.db.get_value(constants.RUN, run, "approval_json"))
		self.assertEqual(
			(facts["proposed_by"], facts["by"], facts["publisher"]),
			(self.proposer, self.admin, self.proposer),
		)

	def test_an_approved_proposal_is_staged_when_its_file_arrives(self):
		path, run, handoff, _result = self.proposed()
		desk.approve_proposed(run, self.code(handoff))
		self.send(path, run, self.proposer)
		frappe.set_user(self.proposer)
		publication_transport.stage_arrived(run)
		self.assertEqual(self.state(run), "Publication staged")

	def test_a_proposal_for_a_new_delegation_shows_its_name_and_makes_its_context_at_approval(self):
		context = str(uuid.uuid4())
		_path, run, handoff = self.sealed(context=context)
		handoff["delegation"] = {"code": "LDP7", "name": "Proposed delegation"}
		frappe.set_user(self.proposer)
		api.propose(handoff)
		frappe.set_user(self.admin)
		self.assertEqual(frappe.db.get_value(constants.RUN, run, "delegation"), "Proposed delegation (LDP7)")
		self.assertFalse(frappe.db.exists("Context", context))  # a proposal prepares nothing
		self.assertFalse(desk.inspect_proposed(run)["context"]["exists"])
		self.assertTrue(desk.approve_proposed(run, self.code(handoff))["context_made"])
		self.assertEqual(frappe.db.get_value("Context", context, "context_code"), "LDP7")

	def test_a_proposal_can_be_declined(self):
		_path, run, _handoff, _result = self.proposed()
		self.assertEqual(api.discard(run)["state"], "Discarded")
		self.assertRaisesRegex(frappe.ValidationError, "not a proposal", desk.inspect_proposed, run)
		frappe.set_user(self.proposer)
		self.assertRaises(frappe.PermissionError, publication_transport.status, run)

	def test_a_proposal_for_a_delegation_the_site_does_not_know_can_be_declined(self):
		context = str(uuid.uuid4())
		_path, run, handoff = self.sealed(context=context)
		handoff["delegation"] = {"code": "LDP8", "name": "Declined delegation"}
		frappe.set_user(self.proposer)
		api.propose(handoff)
		frappe.set_user(self.admin)
		self.assertEqual(api.discard(run)["state"], "Discarded")
		# the list still names the delegation as its hand-off did
		self.assertEqual(frappe.db.get_value(constants.RUN, run, "delegation"), "Declined delegation (LDP8)")
		self.assertFalse(frappe.db.exists("Context", context))

	def test_a_declined_proposal_is_not_approved_and_no_file_is_accepted_for_it(self):
		_path, run, handoff, _result = self.proposed()
		api.discard(run)
		self.assertRaisesRegex(
			frappe.ValidationError, "already on the site", operator.approve, handoff, self.proposer
		)
		self.assertFalse((publication_transport.folder(run) / "approval.json").exists())

	def test_several_proposals_wait_for_one_delegation_and_one_is_approved(self):
		_path, first, first_handoff, _result = self.proposed()
		_other, second, second_handoff, _result = self.proposed(persons=[str(uuid.uuid4())])
		self.assertIsNone(desk.inspect_proposed(second)["context"]["problem"])
		number = desk.approve_proposed(first, self.code(first_handoff))["display_id"]
		self.assertIn(f"Run {number} already holds", desk.inspect_proposed(second)["context"]["problem"])
		self.assertRaisesRegex(
			frappe.ValidationError,
			"already holds this delegation",
			desk.approve_proposed,
			second,
			self.code(second_handoff),
		)

	def test_an_account_that_proposes_cannot_also_approve(self):
		both = make_account("proposer-and-admin", operator=True, publisher=True)
		_path, run, handoff = self.sealed()
		frappe.set_user(both)
		api.propose(handoff)
		self.assertRaisesRegex(
			frappe.ValidationError,
			"cannot be the one that approves",
			desk.approve_proposed,
			run,
			self.code(handoff),
		)
		frappe.set_user(self.admin)
		self.assertRaisesRegex(
			frappe.ValidationError,
			"cannot be the publishing account",
			desk.approve_proposed,
			run,
			self.code(handoff),
		)
		self.assertEqual(self.state(run), "Proposed")


class TestContext(OperatorCase):
	def test_a_delegation_without_a_context_gets_one_locked_and_hidden(self):
		context = str(uuid.uuid4())
		_path, run, handoff = self.sealed(context=context)
		handoff["delegation"] = {"code": "LDN7", "name": "A new delegation"}
		found = operator.inspect_handoff(handoff)
		self.assertEqual((found["context"]["exists"], found["delegation"]["code"]), (False, "LDN7"))
		self.assertFalse(frappe.db.exists("Context", context))
		result = operator.approve(handoff, self.publisher)
		self.assertTrue(result["context_made"])
		made = frappe.db.get_value(
			"Context",
			context,
			["context_code", "context_name", "context_status", "context_discoverability"],
			as_dict=True,
		)
		self.assertEqual((made.context_code, made.context_name), ("LDN7", "A new delegation"))
		self.assertEqual((made.context_status, made.context_discoverability), ("Locked", "Hidden"))
		self.assertEqual(frappe.db.get_value(constants.RUN, run, "delegation"), "A new delegation (LDN7)")

	def test_the_approver_names_a_delegation_the_hand_off_does_not(self):
		context = str(uuid.uuid4())
		_path, _run, handoff = self.sealed(context=context)
		self.assertRaisesRegex(
			frappe.ValidationError, "Give the code and the name", operator.approve, handoff, self.publisher
		)
		operator.approve(handoff, self.publisher, context_code="LDN8", context_name="Named by hand")
		self.assertEqual(frappe.db.get_value("Context", context, "context_code"), "LDN8")

	def test_a_context_that_is_open_is_refused(self):
		_path, run, handoff = self.sealed()
		frappe.db.set_value("Context", self.context, "context_status", "Active")
		found = operator.inspect_handoff(handoff)
		self.assertIn("not locked and hidden", found["context"]["problem"])
		self.assertRaisesRegex(
			frappe.ValidationError, "not locked and hidden", operator.approve, handoff, self.publisher
		)
		self.assertFalse(frappe.db.exists(constants.RUN, run))

	def test_a_context_that_holds_records_is_refused(self):
		from ucm.tests.utils import make_person

		_path, _run, handoff = self.sealed()
		frappe.set_user("Administrator")
		make_person(self.context)
		frappe.set_user(self.admin)
		self.assertIn("already holds Person records", operator.inspect_handoff(handoff)["context"]["problem"])
		self.assertRaisesRegex(
			frappe.ValidationError, "already holds Person records", operator.approve, handoff, self.publisher
		)

	def test_a_delegation_another_run_holds_is_refused(self):
		_path, _run, handoff = self.sealed()
		first = operator.approve(handoff, self.publisher)
		_other_path, _other, other_handoff = self.sealed(persons=[str(uuid.uuid4())])
		problem = operator.inspect_handoff(other_handoff)["context"]["problem"]
		self.assertIn(f"Run {first['display_id']} already holds this delegation", problem)
		self.assertRaisesRegex(
			frappe.ValidationError,
			"already holds this delegation",
			operator.approve,
			other_handoff,
			self.publisher,
		)


class TestStagingAfterArrival(OperatorCase):
	def test_the_last_piece_queues_the_staging(self):
		path, run, handoff = self.sealed()
		operator.approve(handoff, self.publisher)
		with patch.object(frappe, "enqueue") as queued:
			self.send(path, run)
		queued.assert_called_once()
		job = queued.call_args
		self.assertEqual(job.args[0], "ucm_migration_loader.publication_transport.stage_arrived")
		self.assertEqual((job.kwargs["run"], job.kwargs["queue"]), (run, constants.QUEUE))
		self.assertTrue(job.kwargs["enqueue_after_commit"] and job.kwargs["deduplicate"])

	def test_a_file_approved_without_a_run_is_staged_by_hand_only(self):
		"""The older way, from the bench: an approval that left no run."""
		path, run, handoff = self.sealed()
		frappe.set_user("Administrator")
		publication_transport.approve(run, self.publisher, handoff["file_sha256"], handoff["file_bytes"])
		with patch.object(frappe, "enqueue") as queued:
			self.send(path, run)
		queued.assert_not_called()
		self.assertEqual(publication_transport.receive(run)["state"], "Publication staged")

	def test_the_job_stages_the_arrived_file_as_the_approver(self):
		path, run, handoff = self.sealed()
		number = operator.approve(handoff, self.publisher)["display_id"]
		self.send(path, run)
		frappe.set_user(self.publisher)  # the job is queued by the sender's last piece
		publication_transport.stage_arrived(run)
		self.assertEqual(frappe.session.user, self.admin)
		record = frappe.get_doc(constants.RUN, run)
		self.assertEqual((record.state, record.display_id), ("Publication staged", number))
		self.assertEqual((record.publishing_user, record.finding), (self.publisher, None))
		self.assertEqual(json.loads(record.approval_json)["by"], self.admin)
		self.assertEqual(record.write_route, "Document API")
		people = run_overview.build(record)["facts"]
		self.assertEqual((people["approved_by"], people["staged_by"]), (self.admin, self.admin))
		self.assertTrue(people["approved_on"] <= people["staged_on"])
		self.assertIsNone(people["proposed_by"])
		# the job again, or asked from the Desk: the same staged run
		publication_transport.stage_arrived(run)
		self.assertEqual(desk.stage_now(run)["outcome"], "staged")

	def test_the_pieces_are_removed_once_the_file_is_staged(self):
		path, run, handoff = self.sealed()
		operator.approve(handoff, self.publisher)
		self.send(path, run)
		publication_transport.stage_arrived(run)
		frappe.set_user(self.admin)
		inbox = publication_transport.folder(run)
		self.assertTrue(any(piece.name.isdigit() for piece in inbox.iterdir()))
		# what the staging does once it is committed
		publication_transport.clear_pieces(run)
		self.assertEqual(
			sorted(piece.name for piece in inbox.iterdir()), ["approval.json", publication_transport.STAGED]
		)
		# asked again, the site still says the whole file arrived, and the run is the staged one
		frappe.set_user(self.publisher)
		arrived = publication_transport.status(run)
		self.assertEqual(arrived["offset"], arrived["size"])
		frappe.set_user(self.admin)
		self.assertEqual(publication_transport.receive(run)["state"], "Publication staged")
		self.assertEqual(desk.stage_now(run)["outcome"], "staged")

	def test_a_file_the_site_refuses_becomes_a_refused_run(self):
		path, run, handoff = self.sealed(versions={"ucm": "0.0.0-not-this-site"})
		operator.approve(handoff, self.publisher)
		self.send(path, run)
		# the refusal is committed by the receiver; the test keeps it in its own transaction
		with patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"):
			publication_transport.stage_arrived(run)
		record = frappe.get_doc(constants.RUN, run)
		self.assertEqual(record.state, "Rejected")
		self.assertEqual(record.finding, "Preflight refused: application versions differ")
		with patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"):
			result = desk.stage_now(run)
		self.assertEqual(result["outcome"], "refused")
		text, code = said(result, run)
		self.assertEqual(code, 1)
		self.assertIn(f"Refused, and kept as {record.display_id}", text)

	def test_a_staging_that_fails_is_noted_on_the_waiting_run(self):
		path, run, handoff = self.sealed()
		operator.approve(handoff, self.publisher)
		self.send(path, run)
		failure = ValueError("Transfer differs from the approved file")
		with (
			patch.object(publication_transport, "receive", side_effect=failure),
			patch.object(frappe.db, "commit"),
			patch.object(frappe.db, "rollback"),
		):
			self.assertRaises(ValueError, publication_transport.stage_arrived, run)
		record = frappe.get_doc(constants.RUN, run)
		self.assertEqual(record.state, AWAITING)
		self.assertEqual(record.finding, "Staging failed: Transfer differs from the approved file")
		# asked again from the Desk, once the cause is gone
		self.assertEqual(desk.stage_now(run)["outcome"], "staged")
		self.assertIsNone(frappe.db.get_value(constants.RUN, run, "finding"))

	def test_an_approver_who_lost_the_role_stages_nothing(self):
		leaving = make_account("leaving-admin", operator=True)
		frappe.set_user(leaving)
		path, run, handoff = self.sealed()
		operator.approve(handoff, self.publisher)
		self.send(path, run)
		frappe.set_user("Administrator")
		frappe.get_doc("User", leaving).remove_roles("Migration Admin")
		with patch.object(frappe.db, "commit"), patch.object(frappe.db, "rollback"):
			self.assertRaises(frappe.PermissionError, publication_transport.stage_arrived, run)
		self.assertEqual(self.state(run), AWAITING)
		self.assertEqual(
			frappe.db.get_value(constants.RUN, run, "finding"), "Staging failed: PermissionError"
		)

	def test_a_file_that_has_not_arrived_is_not_staged(self):
		path, run, handoff = self.sealed()
		operator.approve(handoff, self.publisher)
		result = desk.stage_now(run)
		self.assertEqual(
			(result["outcome"], result["arrived"], result["bytes"]), ("incomplete", 0, path.stat().st_size)
		)
		self.assertEqual(self.state(run), AWAITING)
		text, code = said(result, run)
		self.assertEqual(code, 1)
		self.assertIn("has not arrived completely", text)

	def test_a_run_never_approved_here_is_said_so(self):
		_path, run, _handoff = self.sealed()
		result = operator.stage_arrived(run)
		self.assertEqual(result, {"outcome": "not approved", "run": run})
		self.assertIn("was not approved on this site", said(result, run)[0])


class TestDeskCalls(OperatorCase):
	def test_the_desk_sends_the_hand_off_as_the_browser_read_it(self):
		_path, run, handoff = self.sealed()
		found = desk.inspect_handoff(json.dumps(handoff))
		self.assertEqual(found["run"], run)
		approved = desk.approve_handoff(json.dumps(handoff), self.publisher)
		self.assertEqual((approved["run"], self.state(run)), (run, AWAITING))

	def test_a_file_that_is_no_hand_off_is_said_so(self):
		for text in ("not json", "[1, 2]", ""):
			self.assertRaisesRegex(frappe.ValidationError, "not a hand-off", desk.inspect_handoff, text)

	def test_who_reads_a_run_reads_its_overview(self):
		_path, run, handoff = self.sealed()
		operator.approve(handoff, self.publisher)
		self.assertEqual(desk.overview(run)["state"], AWAITING)
		frappe.set_user(self.publisher)
		self.assertRaises(frappe.PermissionError, desk.overview, run)
