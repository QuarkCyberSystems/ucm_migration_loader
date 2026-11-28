"""The files of a delegation's persons that a publication cannot carry, as the sealing finds them in the
publishing database. Here the site's own database stands in for it, with synthetic persons and file records."""

import frappe
from ucm.tests.utils import UCMTestCase, make_context, make_person

from ucm_migration_loader import persons, runs
from ucm_migration_loader.source_inventory import person_files


class TestPersonFiles(UCMTestCase):
	codes = iter(range(1000))

	def setUp(self):
		frappe.set_user("Administrator")
		self.context = make_context(f"LDF{next(self.codes)}")

	def person(self, picture: str | None = None) -> str:
		name = make_person(self.context).name
		if picture:
			# only the columns the sealing reads: the picture's own file is not written to the disk
			frappe.db.set_value("Person", name, "person_picture", picture, update_modified=False)
		return name

	def attach(self, person: str, url: str, field: str | None = "person_picture", private: int = 1) -> None:
		frappe.get_doc(
			{
				"doctype": "File",
				"file_name": url.rsplit("/", 1)[-1],
				"file_url": url,
				"is_private": private,
				"attached_to_doctype": "Person",
				"attached_to_name": person,
				"attached_to_field": field,
			}
		).db_insert()

	def found(self) -> dict:
		with frappe.db._conn.cursor() as cur:
			return person_files(cur, self.context)

	def test_persons_with_their_picture_only_are_carried(self):
		self.person()
		with_picture = self.person("/private/files/synthetic-a.jpg")
		self.attach(with_picture, "/private/files/synthetic-a.jpg")
		self.assertEqual(self.found(), {})

	def test_a_file_beside_the_picture_is_found(self):
		first, second = sorted((self.person("/private/files/synthetic-b.jpg"), self.person()))
		for person in (first, second):
			self.attach(person, "/private/files/synthetic-letter.pdf", field=None)
		self.attach(first, "/private/files/synthetic-b.jpg")
		self.attach(first, "/private/files/synthetic-old-picture.jpg")
		self.assertEqual(
			self.found(), {"other_attachments": {"files": 3, "persons": 2, "first_person": first}}
		)

	def test_a_picture_without_its_file_twice_or_public_is_found(self):
		missing = self.person("/private/files/synthetic-c.jpg")
		twice = self.person("/private/files/synthetic-d.jpg")
		for _copy in range(2):
			self.attach(twice, "/private/files/synthetic-d.jpg")
		public = self.person("/files/synthetic-e.jpg")
		self.attach(public, "/files/synthetic-e.jpg", private=0)
		self.assertEqual(
			self.found(),
			{
				"pictures_without_one_private_file": {
					"persons": 3,
					"first_person": min(missing, twice, public),
				}
			},
		)

	def test_the_persons_of_another_delegation_are_not_counted(self):
		person = self.person()
		self.attach(person, "/private/files/synthetic-letter.pdf", field=None)
		other = make_context(f"LDF{next(self.codes)}")
		with frappe.db._conn.cursor() as cur:
			self.assertEqual(person_files(cur, other), {})


class TestPublishedPersonFiles(UCMTestCase):
	"""The site's own check, for a file sealed otherwise: it says what is wrong in a sentence the run shows."""

	def test_a_person_published_with_a_file_beside_its_picture_is_refused_in_a_sentence(self):
		picture = {"file_url": "/private/files/synthetic-a.jpg"}
		letter = {"file_url": "/private/files/synthetic-letter.pdf"}
		for row, files in (
			({"person_picture": "/private/files/synthetic-a.jpg"}, [picture, letter]),
			({"person_picture": None}, [letter]),
			({"person_picture": "/private/files/synthetic-a.jpg"}, []),
			({"person_picture": "/private/files/synthetic-a.jpg"}, [letter]),
		):
			with self.assertRaises(runs.Unpublishable) as refused:
				persons.prepare_person(row, {}, files)
			self.assertEqual(str(refused.exception), persons.FILES_DIFFER)
			self.assertIsInstance(refused.exception, runs.Refusal)
