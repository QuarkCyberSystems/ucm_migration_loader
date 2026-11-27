"""Validated SQL writes for sealed delegation publications."""

from collections import defaultdict
from itertools import groupby

import frappe
from frappe.core.doctype.file.utils import relink_mismatched_files
from frappe.desk.form.document_follow import _follow_document

from .batch_metrics import measure
from .frozen import ORDER, OWNERS

NOTE_TYPES = frozenset(("Person Note", "Activity Note", "Activity Detail Note"))


@measure("validation")
def prepare_insert(doc, name, *, set_child_names=False):
	# The before-write lifecycle of Document.insert from this bench's Frappe release.
	doc.flags.notifications_executed = []
	doc.set("__islocal", True)
	doc._set_defaults()
	doc.set_user_and_timestamp()
	doc.set_docstatus()
	doc.check_permission("create")
	doc.check_if_latest()
	doc._validate_links()
	doc.run_method("before_insert")
	doc.set_new_name(set_name=name, set_child_names=set_child_names)
	doc.set_parent_in_children()
	doc.validate_higher_perm_levels()
	doc.flags.in_insert = True
	doc.run_before_save_methods()
	doc._validate()
	doc.set_docstatus()
	doc.flags.in_insert = False


def finish_insert(doc):
	doc.reset_computed_child_tables()
	doc.run_method("after_insert")
	doc.flags.in_insert = True
	if doc.get("amended_from"):
		doc.validate_amended_from()
		doc.copy_attachments_from_amended_from()
	relink_mismatched_files(doc)
	doc.run_post_save_methods()
	doc.flags.in_insert = False
	for field in ("__islocal", "__unsaved"):
		if hasattr(doc, field):
			delattr(doc, field)
	if not (
		frappe.flags.in_migrate or frappe.local.flags.in_install or frappe.flags.in_setup_wizard
	) and frappe.get_cached_value("User", frappe.session.user, "follow_created_documents"):
		_follow_document(doc.doctype, doc.name, frappe.session.user)


def insert_document(doc, *, set_name, set_child_names=True, ignore_permissions=False):
	"""Flush this aggregate before hooks or the next dependent record can query it."""
	doc.flags.ignore_permissions = ignore_permissions
	prepare_insert(doc, set_name, set_child_names=set_child_names)
	for dt, group in groupby([doc, *doc.get_all_children()], key=lambda row: row.doctype):
		persist_rows(dt, list(group))
	finish_insert(doc)
	return doc


def persist_rows(dt, rows):
	values = [row.get_valid_dict(convert_dates_to_str=True, ignore_virtual=True) for row in rows]
	fields = list(values[0])
	frappe.db.bulk_insert(dt, fields, (tuple(value[field] for field in fields) for value in values))
	for row in rows:
		row.set("__islocal", False)


def write_person_batch(db, context, items, sequence):
	from .persons import finish_person, prepare_person
	from .publication_worker import picture_attacher, prepare_item_rows, restore_audit

	cache = {}
	actors = set()
	pending = []
	pending_names = set()

	def flush():
		if not pending:
			return
		parents = [entry[1] for entry in pending]
		persist_rows("Person", parents)
		children = defaultdict(list)
		for doc in parents:
			for child in doc.get_all_children():
				children[child.doctype].append(child)
		for dt, rows in children.items():
			persist_rows(dt, rows)
		for position, doc, item, row, nested in pending:
			try:
				finish_insert(doc)
				files = item["files"]
				attach = picture_attacher(db, cache, files[0]["sha256"] if files else None, insert_document)
				finish_person(doc, row, nested, [f["row"] for f in files], attach)
				for f in files:
					restore_audit("File", f["row"], actors)
				for child_type, child_rows in item["children"].items():
					for child_row in child_rows:
						restore_audit(child_type, child_row, actors)
				restore_audit("Person", item["row"], actors)
			except Exception as exc:
				exc.migration_location = (sequence, position, "Person")
				raise
		pending.clear()
		pending_names.clear()

	for position, item in enumerate(items, 1):
		# A link to an earlier Person must resolve during normal link validation.
		records = [
			item["row"],
			*(r for rows in item["children"].values() for r in rows),
		]
		if any(isinstance(v, str) and v in pending_names for r in records for v in r.values()):
			flush()
		try:
			if item["doctype"] != "Person":
				raise ValueError("Mixed Person batch")
			row, nested = prepare_item_rows(db, cache, context, item)
			doc = prepare_person(row, nested, [f["row"] for f in item["files"]])
			prepare_insert(doc, row["name"])
			pending.append((position, doc, item, row, nested))
			pending_names.add(row["name"])
		except Exception as exc:
			exc.migration_location = (sequence, position, "Person")
			raise
	flush()


def write_batch(db, context, items, sequence):
	from .publication_worker import checked_row, mapped_row, restore_audit

	dt = items[0]["doctype"]
	if dt not in ORDER:
		raise ValueError("Bulk SQL is unavailable for this DocType")
	if dt == "Person":
		return write_person_batch(db, context, items, sequence)
	if dt not in NOTE_TYPES:
		from .publication_worker import insert_batch

		return insert_batch(db, context, items, sequence, insert=insert_document)
	cache = {}
	actors = set()
	parent, key = OWNERS[dt]
	documents = []
	for position, item in enumerate(items, 1):
		try:
			raw = item["row"]
			if item["doctype"] != dt or item["children"] or item.get("files"):
				raise ValueError("Unexpected bulk SQL batch contents")
			if (
				raw["context_tech_id"] != context
				or frappe.db.get_value(parent, raw[key], "context_tech_id") != context
			):
				raise ValueError("Published note or parent belongs to another delegation")
			row = mapped_row(db, cache, dt, raw)
			doc = frappe.get_doc({"doctype": dt, **row})
			prepare_insert(doc, raw["name"])
			if doc.get_all_children():
				raise ValueError("Bulk SQL note writer cannot persist child rows")
			values = doc.get_valid_dict(convert_dates_to_str=True, ignore_virtual=True)
			documents.append((doc, row, raw, values))
		except Exception as exc:
			exc.migration_location = (sequence, position, dt)
			raise
	fields = list(documents[0][3])
	if any(list(values) != fields for _, _, _, values in documents):
		raise ValueError("Inconsistent bulk insert columns")
	frappe.db.bulk_insert(dt, fields, (tuple(values.values()) for _, _, _, values in documents))
	for position, (doc, row, raw, _) in enumerate(documents, 1):
		try:
			doc.set("__islocal", False)
			finish_insert(doc)
			if dt in ("Activity Note", "Activity Detail Note"):
				row.pop("activity_tags", None)
				row.pop("person_tags", None)
			checked_row(dt, row)
			restore_audit(dt, raw, actors)
		except Exception as exc:
			exc.migration_location = (sequence, position, dt)
			raise
