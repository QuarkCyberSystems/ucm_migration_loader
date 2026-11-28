"""A discard removes a delegation's rows itself and calls no delete of Frappe, so it also removes what Frappe keeps
about each record. These tests hold that list against Frappe's own, and check what is removed."""

import ast
import inspect
import uuid

import frappe
from frappe.model import delete_doc
from frappe.tests import IntegrationTestCase

from ucm_migration_loader.ucm_migration_loader import set_based

DEFAULT = ("reference_doctype", "reference_name")


def frappe_cleans() -> tuple[set, set]:
	"""What Frappe's deferred cleanup deletes and what it unlinks, read from its source: (table, the column of
	the DocType, the column of the name)."""
	deleted, unlinked = set(), set()
	tree = ast.parse(inspect.getsource(delete_doc.delete_dynamic_links))
	for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
		named = call.func.id if isinstance(call.func, ast.Name) else ""
		texts = [arg.value for arg in call.args if isinstance(arg, ast.Constant)]
		if named == "clear_timeline_references":
			deleted.add(("Communication Link", "link_doctype", "link_name"))
		elif named in ("delete_references", "clear_references"):
			# the two columns are optional arguments, each with Frappe's default
			table, columns = texts[0], (*texts[1:], *DEFAULT[len(texts) - 1 :])
			(deleted if named == "delete_references" else unlinked).add((table, *columns))
	return deleted, unlinked


class TestDiscardLeavesNothingOfFrappe(IntegrationTestCase):
	def test_the_list_is_the_one_frappe_cleans(self):
		"""A Frappe that cleans another table after a delete fails this test: add the table to the discard."""
		deleted, unlinked = frappe_cleans()
		self.assertGreaterEqual(len(deleted), 9)
		self.assertEqual(set(set_based.LINKED), deleted)
		self.assertEqual(set(set_based.UNLINKED), unlinked)

	def test_what_frappe_keeps_about_a_record_goes_with_it(self):
		gone, kept = str(uuid.uuid4()), str(uuid.uuid4())
		for name in (gone, kept):
			for values in (
				{"doctype": "Comment", "comment_type": "Comment", "content": "synthetic"},
				{"doctype": "View Log", "viewed_by": "Administrator"},
				{"doctype": "Activity Log", "subject": "synthetic"},
				{"doctype": "Version", "data": "{}"},
			):
				columns = ("ref_doctype", "docname") if values["doctype"] == "Version" else DEFAULT
				row = frappe.get_doc({**values, columns[0]: "Person", columns[1]: name})
				# written as rows: the records they speak of are not there, as after a load that is being discarded
				row.name = str(uuid.uuid4())
				row.db_insert()
			frappe.db.sql(
				"insert into `__global_search` (doctype, name, content) values (%s, %s, %s)",
				("Person", name, "synthetic"),
			)

		def held(table, kind, column, name):
			return frappe.db.count(table, {kind: "Person", column: name})

		set_based._forget("Person", (gone,))
		for table, kind, column in (
			("Comment", *DEFAULT),
			("View Log", *DEFAULT),
			("Version", "ref_doctype", "docname"),
			("Activity Log", *DEFAULT),
		):
			self.assertEqual(held(table, kind, column, gone), 0, table)
			self.assertEqual(held(table, kind, column, kept), 1, table)
		search = "select count(*) from `__global_search` where doctype=%s and name=%s"
		self.assertEqual(frappe.db.sql(search, ("Person", gone))[0][0], 0)
		self.assertEqual(frappe.db.sql(search, ("Person", kept))[0][0], 1)
		# an activity log is kept and unlinked, as Frappe does
		self.assertEqual(frappe.db.count("Activity Log", {"subject": "synthetic"}), 2)

	def test_a_discard_calls_no_delete_of_frappe(self):
		"""Frappe's delete queues a job that acts later, on whatever then bears the record's identifier."""
		source = inspect.getsource(set_based)
		self.assertNotIn("delete_doc(", source)
		self.assertNotIn(".delete(", source)
