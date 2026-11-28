"""The set-based route's reading of UCM's rules, held against UCM itself.

No controller runs for a row the route copies. Two things keep its rules in step with UCM's:

- the code a save runs for the DocTypes it writes is the code the route was last reviewed against (ucm_rules);
- for the rules the route states again in SQL, the documents and the queries agree on the same records: what a
  save writes breaks no rule, what a save refuses is found by a rule, and what a save derives is derived alike.

Run nightly against UCM's develop branch by the CI: a change in UCM that the route has to follow fails here first.
"""

import uuid
from contextlib import contextmanager

import frappe
from frappe.tests import IntegrationTestCase
from ucm.tests.utils import DepartmentsCase, make_person

from ucm_migration_loader.ucm_migration_loader import set_based, ucm_rules

REFUSALS = (frappe.ValidationError, frappe.PermissionError)


class TestReviewedRules(IntegrationTestCase):
	def test_ucm_runs_the_rules_the_route_was_reviewed_against(self):
		changed = ucm_rules.differences(ucm_rules.fingerprints(), ucm_rules.reviewed()["rules"])
		self.assertFalse(
			changed,
			"UCM's save rules differ from the ones the set-based route was reviewed against "
			f"(UCM {ucm_rules.reviewed()['ucm']}). Read each change, bring set_based in line where it copies "
			"the rule, then keep the new reading with ucm_rules.review:\n  " + "\n  ".join(changed),
		)

	def test_every_table_the_route_writes_is_read(self):
		"""A DocType whose controller is not found would have no rule to compare, and pass for ever."""
		unread = [doctype for doctype, rules in ucm_rules.fingerprints().items() if not rules]
		self.assertEqual(unread, [])
		self.assertIn("Person Relationship", ucm_rules.written_doctypes())
		self.assertNotIn("Record Share", ucm_rules.written_doctypes())


class TestRulesAgainstDocuments(DepartmentsCase):
	"""Records made through UCM's documents in a context of the test, read by the route's queries."""

	def setUp(self):
		super().setUp()
		self.kind = self.place_type("LDR prison")
		self.other_kind = self.place_type("LDR camp")
		self.country = frappe.db.get_value("Country Territory", {})
		self.prison = self.place("LDR prison A", self.kind, self.ctx, self.country)
		self.camp = self.place("LDR camp A", self.other_kind, self.ctx)
		# a type that records a place of any type, and one that asks for a prison
		self.anywhere = self.activity_type("LDR anywhere")
		self.visit_type = self.activity_type("LDR visit")
		frappe.db.set_value("Activity Type", self.anywhere.name, "activity_type_enabled_place", 1)
		frappe.db.set_value(
			"Activity Type",
			self.visit_type.name,
			{"activity_type_enabled_place": 1, "activity_type_place_type_tech_id": self.kind},
		)
		self.base = {
			"Person": {"doctype": "Person", "context_tech_id": self.ctx, "person_name": "LDR person"},
			"Case": {
				"doctype": "Case",
				"context_tech_id": self.ctx,
				"case_name": "LDR case",
				"case_status": "Open",
			},
			"Activity": {
				"doctype": "Activity",
				"context_tech_id": self.ctx,
				"activity_name": "LDR activity",
				"activity_type_tech_id": self.anywhere.name,
				"service_type_tech_id": self.tracing.name,
			},
		}

	# --- records ---------------------------------------------------------------------------------

	def place_type(self, name):
		return frappe.db.get_value("Place Type", {"place_type_name": name}) or (
			frappe.get_doc({"doctype": "Place Type", "place_type_name": name}).insert().name
		)

	def place(self, name, kind, context, country=None):
		return (
			frappe.get_doc(
				{
					"doctype": "Place",
					"context_tech_id": context,
					"place_name": f"{name} {frappe.generate_hash(length=6)}",
					"place_type_tech_id": kind,
					"place_addresses": [{"country_tech_id": country}] if country else [],
				}
			)
			.insert()
			.name
		)

	def saved(self, doctype, **values):
		return frappe.get_doc({**self.base[doctype], **values}).insert()

	# --- the two readings ------------------------------------------------------------------------

	def broken(self, doctype) -> dict:
		"""The route's rules that the context's rows of a table break."""
		return set_based.integrity(self.ctx, [doctype])["broken"].get(doctype, {})

	def refused(self, values: dict) -> None:
		"""UCM's document refuses the record."""
		frappe.db.savepoint("refused")
		try:
			with self.assertRaises(REFUSALS):
				frappe.get_doc(values).insert()
		finally:
			frappe.db.rollback(save_point="refused")

	@contextmanager
	def stored(self, doctype: str, name: str, **values):
		"""The record with these values written by SQL, as a publication would carry them, for the block."""
		frappe.db.savepoint("stored")
		frappe.db.set_value(doctype, name, values, update_modified=False)
		try:
			yield
		finally:
			frappe.db.rollback(save_point="stored")

	def agree(self, doctype: str, rule: str, **values) -> None:
		"""What a save refuses, the rule finds; and it finds nothing in what the save wrote."""
		self.refused({**self.base[doctype], **values})
		record = self.saved(doctype)
		self.assertNotIn(rule, self.broken(doctype))
		with self.stored(doctype, record.name, **values):
			self.assertIn(rule, self.broken(doctype), f"{doctype}: no rule finds {values}")

	# --- what a save writes breaks no rule -------------------------------------------------------

	def test_records_saved_by_ucm_break_no_rule(self):
		person, relative = self.saved("Person"), make_person(self.ctx, "LDR relative")
		case = self.saved("Case")
		frappe.get_doc(
			{
				"doctype": "Person Relationship",
				"context_tech_id": self.ctx,
				"person_tech_id": person.name,
				"person_related_tech_id": relative.name,
			}
		).insert()
		frappe.get_doc(
			{
				"doctype": "Case Member",
				"context_tech_id": self.ctx,
				"case_tech_id": case.name,
				"person_tech_id": person.name,
			}
		).insert()
		frappe.get_doc(
			{
				"doctype": "Person Note",
				"context_tech_id": self.ctx,
				"person_tech_id": person.name,
				"person_note": "LDR note",
			}
		).insert()
		self.saved("Activity", activity_type_tech_id=self.visit_type.name, activity_place_tech_id=self.prison)
		self.saved("Activity")
		for doctype in ("Person", "Case", "Case Member", "Person Relationship", "Person Note", "Activity"):
			self.assertEqual(self.broken(doctype), {}, doctype)

	# --- what a save refuses, a rule finds -------------------------------------------------------

	def test_a_value_outside_a_select_fields_options(self):
		checked = 0
		for doctype in self.base:
			for df in frappe.get_meta(doctype).get("fields", {"fieldtype": "Select"}):
				if df.options and not df.read_only:
					self.agree(doctype, f"{df.fieldname}: not an option", **{df.fieldname: "Not an option"})
					checked += 1
		self.assertGreater(checked, 0)

	def test_a_required_field_left_empty(self):
		checked = 0
		for doctype, base in self.base.items():
			for df in frappe.get_meta(doctype).fields:
				field = df.fieldname
				if not df.reqd or df.mandatory_depends_on or field == "context_tech_id" or field not in base:
					continue
				self.agree(doctype, f"{field}: required", **{field: None})
				checked += 1
		self.assertGreater(checked, 0)

	def test_an_option_set_field_linked_to_nothing_or_to_another_list(self):
		from ucm.utils.data_model import option_sets

		field, concept = option_sets()["Person"][0]
		self.agree("Person", f"{field}: link to nothing", **{field: str(uuid.uuid4())})
		other = frappe.db.get_value(
			"Concept Value", {"concept_urn": ["!=", concept], "concept_value_usable": 1}, "name"
		)
		if not other:
			self.skipTest("the site's dictionary holds no value of another list")
		self.agree("Person", f"{field}: not an active value of its list", **{field: other})

	def test_a_link_to_another_contexts_record(self):
		elsewhere = self.place("LDR elsewhere", self.kind, self.other_ctx)
		self.agree(
			"Activity", "activity_place_tech_id: belongs to another context", activity_place_tech_id=elsewhere
		)

	def test_a_place_where_the_activity_type_records_none(self):
		self.agree(
			"Activity",
			"place: its activity type records none",
			activity_type_tech_id=self.visit.name,
			activity_place_tech_id=self.prison,
		)

	def test_a_place_of_another_type_than_the_activity_type_asks_for(self):
		self.agree(
			"Activity",
			"place: not of the place type its activity type asks for",
			activity_type_tech_id=self.visit_type.name,
			activity_place_tech_id=self.camp,
		)

	def test_a_missing_place_the_activity_type_requires(self):
		rule = "place: required by its activity type"
		recorded = self.saved("Activity", activity_type_tech_id=self.visit_type.name)
		self.assertNotIn(rule, self.broken("Activity"))
		frappe.db.savepoint("required")
		try:
			frappe.db.set_value(
				"Activity Type",
				self.visit_type.name,
				"activity_type_place_required",
				1,
				update_modified=False,
			)
			self.refused({**self.base["Activity"], "activity_type_tech_id": self.visit_type.name})
			self.assertEqual(self.broken("Activity").get(rule), 1, recorded.name)
		finally:
			frappe.db.rollback(save_point="required")

	def test_a_persons_relationship_to_another_recorded_twice(self):
		rule = "related person: this person's relationship to them is recorded twice"
		person, relative, third = (make_person(self.ctx, f"LDR kin {n}") for n in range(3))
		pair = {"doctype": "Person Relationship", "context_tech_id": self.ctx, "person_tech_id": person.name}
		frappe.get_doc({**pair, "person_related_tech_id": relative.name}).insert()
		second = frappe.get_doc({**pair, "person_related_tech_id": third.name}).insert()
		# the relationship the other way round is another row
		frappe.get_doc(
			{**pair, "person_tech_id": relative.name, "person_related_tech_id": person.name}
		).insert()
		self.assertNotIn(rule, self.broken("Person Relationship"))
		self.refused({**pair, "person_related_tech_id": relative.name})
		with self.stored("Person Relationship", second.name, person_related_tech_id=relative.name):
			self.assertEqual(self.broken("Person Relationship").get(rule), 2)

	# --- what a save derives, the route derives alike --------------------------------------------

	def test_the_place_gives_an_activity_its_place_type_and_address_as_a_save_does(self):
		from ucm.ucm.doctype.activity.activity import ADDRESSES

		fields = ["activity_place_type_tech_id", *ADDRESSES[0]]
		activity = self.saved(
			"Activity", activity_type_tech_id=self.visit_type.name, activity_place_tech_id=self.prison
		)
		by_save = frappe.db.get_value("Activity", activity.name, fields, as_dict=True)
		self.assertEqual(str(by_save.activity_place_type_tech_id), str(self.kind))
		self.assertEqual(str(by_save.activity_place_country_tech_id), str(self.country))
		# as a publisher that left them to the target: empty in the publication
		frappe.db.set_value("Activity", activity.name, dict.fromkeys(fields), update_modified=False)
		self.assertIn("place: place type differs from the Place's", self.broken("Activity"))
		self.assertGreater(set_based._complete_places(self.ctx), 0)
		self.assertEqual(frappe.db.get_value("Activity", activity.name, fields, as_dict=True), by_save)
		self.assertEqual(self.broken("Activity"), {})
		self.assertEqual(set_based._complete_places(self.ctx), 0)
