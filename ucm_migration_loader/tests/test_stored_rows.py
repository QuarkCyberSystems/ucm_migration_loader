"""A stored row that differs from the published one is refused in a sentence the run shows: it names the
fields, never what they hold."""

import frappe
from ucm.tests.utils import UCMTestCase, make_context, make_person

from ucm_migration_loader import runs
from ucm_migration_loader.stored_rows import checked_row


class TestStoredRows(UCMTestCase):
	# the class is rolled back as a whole: each test takes a context of its own
	codes = iter(range(1000))

	def setUp(self):
		frappe.set_user("Administrator")
		context = make_context(f"LDW{next(self.codes)}")
		self.person = make_person(context, name="Synthetic Stored").name

	def test_a_row_stored_as_published_passes(self):
		checked_row("Person", {"name": self.person, "person_name": "Synthetic Stored"})

	def test_fields_that_differ_are_named_without_their_values(self):
		with self.assertRaises(runs.Refusal) as refused:
			checked_row("Person", {"name": self.person, "person_name": "Synthetic Published"})
		self.assertEqual(str(refused.exception), "Published Person fields differ on target: ['person_name']")
		self.assertNotIn("Synthetic", str(refused.exception))
