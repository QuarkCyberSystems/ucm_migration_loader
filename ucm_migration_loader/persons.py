"""Write a published person, its ordered child rows and its private picture through UCM."""

from collections import Counter
from hashlib import sha256
from pathlib import Path

from .batch_metrics import measure
from .stored_rows import checked_row, insert_document


@measure("attachments")
def attach_picture_bytes(person, source_file, content, *, folder=None, insert=insert_document):
	import frappe
	from frappe.core.doctype.file.utils import get_content_hash
	from frappe.utils.file_manager import get_max_file_size

	if (
		source_file["is_private"] != 1
		or not source_file["file_url"].startswith("/private/files/")
		or source_file["attached_to_field"] != "person_picture"
		or source_file["attached_to_doctype"] != "Person"
		or source_file["attached_to_name"] != person.name
		or not 0 < len(content) <= get_max_file_size()
	):
		raise ValueError("Invalid private person picture metadata")
	if len(content) != source_file["file_size"] or get_content_hash(content) != source_file["content_hash"]:
		raise ValueError("Source private picture differs from its File metadata")
	doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": source_file["file_name"],
			"folder": folder,
			"is_private": 1,
			"attached_to_doctype": "Person",
			"attached_to_name": person.name,
			"attached_to_field": "person_picture",
			"content": content,
		}
	)
	insert(doc, set_name=source_file["name"])
	stored = frappe.get_doc("File", source_file["name"])
	if not stored.is_private or (
		stored.attached_to_doctype,
		stored.attached_to_name,
		stored.attached_to_field,
	) != ("Person", person.name, "person_picture"):
		raise ValueError("Stored private attachment ownership differs")
	with Path(stored.get_full_path()).open("rb") as handle:
		from hashlib import file_digest

		if file_digest(handle, "sha256").digest() != sha256(content).digest():
			raise ValueError("Stored private picture bytes differ")
	return stored.file_url


def insert_prepared_person(row, nested, files, attach, *, insert=insert_document):
	doc = prepare_person(row, nested, files)
	insert(doc, set_name=row["name"], set_child_names=False)
	return finish_person(doc, row, nested, files, attach)


def prepare_person(row, nested, files):
	import frappe

	url = row["person_picture"]
	if len(files) != int(bool(url)) or (files and files[0]["file_url"] != url):
		raise ValueError("Published picture and file inventory differ")
	doc = frappe.get_doc(
		{
			"doctype": "Person",
			**{k: v for k, v in row.items() if k != "person_picture"},
			**{k: [dict(r) for r in rows] for k, rows in nested.items()},
		}
	)
	return doc


def finish_person(doc, row, nested, files, attach):
	import frappe

	if files:
		doc.person_picture = attach(doc, files[0])
		doc.save()
		row["person_picture"] = doc.person_picture
	checked_row("Person", row)
	saved_person = frappe.get_doc("Person", row["name"])
	for dt, key in (
		("Person Address", "person_addresses"),
		("Person Other Name", "person_other_names"),
	):
		saved = saved_person.get(key)
		if [str(r.name) for r in saved] != [r["name"] for r in nested[key]]:
			raise ValueError(f"{dt} keys or order differ")
		for child in nested[key]:
			checked_row(dt, child)
	return Counter(
		persons=1,
		pictures=len(files),
		aliases=len(nested["person_other_names"]),
		addresses=len(nested["person_addresses"]),
	)
