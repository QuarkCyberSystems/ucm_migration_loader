"""Reconstitute a generated person, ordered child rows and their private picture through UCM."""

from collections import Counter
from hashlib import sha256
from pathlib import Path

import pymysql

from .batch_metrics import measure
from .dictionary_identity import TERM_COLUMNS, resolve_term
from .stored_rows import business, checked_row, insert_document


def person_records(source, context):
	with source.cursor(pymysql.cursors.DictCursor) as cur:
		last = None
		while True:
			clause = "and name>%s" if last else ""
			cur.execute(
				f"select * from tabPerson where context_tech_id=%s {clause} order by name limit 100",
				(context, last) if last else (context,),
			)
			parents = cur.fetchall()
			if not parents:
				return
			for parent in parents:
				key = parent["name"]
				children = {}
				for dt, field in (
					("Person Address", "person_addresses"),
					("Person Other Name", "person_other_names"),
				):
					cur.execute(
						f"select * from `tab{dt}` where parent=%s order by idx,name",
						key,
					)
					rows = [business(r, child=True) for r in cur.fetchall()]
					if any(
						(r["parenttype"], r["parentfield"], r["idx"]) != ("Person", field, i)
						for i, r in enumerate(rows, 1)
					):
						raise ValueError(f"Invalid published {dt} ownership or order")
					children[dt] = rows
				cur.execute(
					"select * from tabFile where attached_to_doctype='Person' and attached_to_name=%s order by name",
					key,
				)
				yield business(parent), children, cur.fetchall()
			last = parents[-1]["name"]


def map_terms(source, target, row, dt, cache):
	import frappe

	for df in frappe.get_meta(dt).fields:
		value = row.get(df.fieldname)
		if not value or df.fieldtype != "Link" or df.options != "Concept Value":
			continue
		if value not in cache:
			with source.cursor() as cur:
				cur.execute(
					f"select {TERM_COLUMNS} from `tabConcept Value` where name=%s",
					value,
				)
				attributes = cur.fetchone()
			if attributes is None:
				raise ValueError(f"Missing source term for {dt}.{df.fieldname}")
			resolved, status = resolve_term(target, value, attributes)
			if not resolved:
				raise ValueError(f"Dictionary {dt}.{df.fieldname}: {status}")
			cache[value] = resolved
		row[df.fieldname] = cache[value]


def attach_picture(person, source_file, private_root):
	from frappe.utils.file_manager import get_max_file_size

	path = (private_root / source_file["file_url"].removeprefix("/private/files/")).resolve(strict=True)
	if not path.is_relative_to(private_root) or not path.is_file():
		raise ValueError("Invalid private file location")
	with path.open("rb") as handle:
		content = handle.read(get_max_file_size() + 1)
	return attach_picture_bytes(person, source_file, content)


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


def insert_person(source, target, record, private_root, cache):
	import frappe

	frappe.only_for("System Manager")
	parent, children, files = record
	row = dict(parent)
	map_terms(source, target, row, "Person", cache)
	nested = {}
	for dt, key in (
		("Person Address", "person_addresses"),
		("Person Other Name", "person_other_names"),
	):
		rows = [dict(r) for r in children[dt]]
		for child in rows:
			map_terms(source, target, child, dt, cache)
		nested[key] = rows
	return insert_prepared_person(row, nested, files, lambda doc, f: attach_picture(doc, f, private_root))


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
