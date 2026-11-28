"""A share to a delegation that is not open yet is held by the run that published it, with what its release
needs, and is released when the receiving delegation is activated: after the publishing run's file has left
the site."""

import json
import sqlite3
import uuid
from contextlib import closing

import frappe
from ucm.tests.utils import UCMTestCase, make_context, make_person

from ucm_migration_loader import constants, held_shares
from ucm_migration_loader.publication_receiver import local_publication
from ucm_migration_loader.tests.utils import make_account, make_receipt, make_run

PUBLISHED_ON = "2021-01-01T00:00:00"


class TestHeldShares(UCMTestCase):
	# the class is rolled back as a whole: each test takes its own two contexts
	codes = iter(range(1000))

	def setUp(self):
		# only the loader's own methods write its records; the tests stand in for an activation
		frappe.set_user("Administrator")
		self.owner = make_context(f"LDH{next(self.codes)}")
		self.receiver = make_context(f"LDH{next(self.codes)}")
		frappe.db.set_value("Context", self.receiver, "context_status", "Locked")
		self.person = make_person(self.owner).name
		self.run = make_run(state=constants.ACTIVATED, context=self.owner)
		make_receipt(self.run.name, 3, write_route="Document API")
		self.share = str(uuid.uuid4())

	def published(self) -> dict:
		"""A share as a publication carries it: accepted long ago, for a delegation loaded later."""
		return {
			"name": self.share,
			"docstatus": 0,
			"idx": 0,
			"creation": PUBLISHED_ON,
			"modified": PUBLISHED_ON,
			"owner": "Administrator",
			"modified_by": "Administrator",
			"context_tech_id": self.owner,
			"share_doctype": "Person",
			"share_name": self.person,
			"share_to_context_tech_id": self.receiver,
			"share_to_user": None,
			"share_status": "Accepted",
			"share_until": None,
			"share_reason": "Synthetic reason",
			"shared_by": "Administrator",
			"shared_on": PUBLISHED_ON,
			"share_decided_by": "Administrator",
			"share_decided_on": PUBLISHED_ON,
		}

	def hold(self) -> None:
		"""Hold the share as an activation does, with the publication's file at hand."""
		said = frappe.db.get_value("User", "Administrator", ["email", "full_name"], as_dict=True)
		with closing(sqlite3.connect(":memory:")) as db:
			db.execute("create table refs (dt text, id text, payload blob, primary key (dt, id))")
			db.execute("insert into refs values ('User', 'Administrator', ?)", (json.dumps(dict(said)),))
			held_shares.hold(self.run, self.published(), 3, 1, db)

	def test_a_held_share_keeps_its_row_and_what_it_refers_to(self):
		self.hold()
		kept = json.loads(frappe.db.get_value(constants.HELD, self.share, "share_json"))
		self.assertEqual(kept["row"], self.published())
		# named by three of its fields, kept once
		self.assertEqual(
			[(master, value) for master, value, _said in kept["references"]], [("User", "Administrator")]
		)
		self.assertEqual(held_shares.summary(self.run.name), {"Held": 1})

	def test_no_role_reads_what_a_held_share_keeps(self):
		self.hold()
		manager = make_account("held-manager")
		frappe.get_doc("User", manager).add_roles("System Manager")
		for account in (make_account("held-operator", operator=True), manager):
			self.as_user(account)
			record = frappe.get_doc(constants.HELD, self.share)
			record.apply_fieldlevel_read_permissions()
			self.assertEqual(record.state, "Held", account)
			self.assertFalse(record.get("share_json"), account)
			listed = frappe.get_list(
				constants.HELD, filters={"name": self.share}, fields=["name", "share_json"]
			)
			self.assertEqual([row.get("share_json") for row in listed], [None], account)

	def test_a_held_share_is_released_without_its_run_s_file(self):
		self.hold()
		self.assertFalse(local_publication(self.run.name).exists())
		context = frappe.get_doc("Context", self.receiver)
		context.context_status = "Active"
		context.save()
		receiving = make_run(state=constants.READY, context=self.receiver)

		outcomes = held_shares.release_incoming(context, receiving.name)

		self.assertEqual(outcomes, {"Released": 1})
		share = frappe.db.get_value(
			"Record Share",
			self.share,
			["share_name", "share_to_context_tech_id", "share_status", "share_reason", "creation"],
			as_dict=True,
		)
		self.assertEqual(
			(share.share_name, share.share_to_context_tech_id, share.share_status, share.share_reason),
			(self.person, self.receiver, "Accepted", "Synthetic reason"),
		)
		self.assertEqual(share.creation.isoformat(), PUBLISHED_ON)
		held = frappe.db.get_value(
			constants.HELD, self.share, ["state", "release_run", "share_json"], as_dict=True
		)
		self.assertEqual((held.state, held.release_run, held.share_json), ("Released", receiving.name, None))

	def test_a_share_held_without_its_row_stops_the_release(self):
		self.hold()
		frappe.db.set_value(constants.HELD, self.share, "share_json", None)
		frappe.db.set_value("Context", self.receiver, "context_status", "Active")
		receiving = make_run(state=constants.READY, context=self.receiver)
		with self.assertRaisesRegex(ValueError, "recorded without its row"):
			held_shares.release_incoming(frappe.get_doc("Context", self.receiver), receiving.name)
		self.assertFalse(frappe.db.exists("Record Share", self.share))
