"""Experimental validated MariaDB bulk inserts, confined to the named benchmark site."""

from collections import Counter, defaultdict
from time import monotonic

import frappe
from benchmark_publication import SITES

from ucm_migration_loader import publication_worker as worker
from ucm_migration_loader.frozen import CHILDREN, ORDER, OWNERS
from ucm_migration_loader.persons import attach_picture_bytes
from ucm_migration_loader.sql_writer import finish_insert, prepare_insert

TIMES = Counter()
ROWS = Counter()


def prepare(db, cache, context, item):
	dt, raw = item["doctype"], item["row"]
	if raw["context_tech_id"] != context:
		raise ValueError("Published document lies outside the target context")
	if dt in OWNERS:
		parent, key = OWNERS[dt]
		if frappe.db.get_value(parent, raw[key], "context_tech_id") != context:
			raise ValueError("Parent delegation differs")
	if dt == "Activity":
		worker.verify_place_address(db, cache, raw["activity_place_tech_id"])
	row = worker.mapped_row(db, cache, dt, raw)
	if dt == "Person":
		picture = row.pop("person_picture")
		files = item["files"]
		if len(files) != int(bool(picture)) or (files and files[0]["row"]["file_url"] != picture):
			raise ValueError("Published picture metadata differs")
	if set(item["children"]) != set(CHILDREN.get(dt, ())):
		raise ValueError("Unexpected child inventory")
	for child, records in item["children"].items():
		field = next(
			f.fieldname for f in frappe.get_meta(dt).fields if f.fieldtype == "Table" and f.options == child
		)
		previous = 0
		for idx, r in enumerate(records, 1):
			if (r["parent"], r["parenttype"], r["parentfield"]) != (
				raw["name"],
				dt,
				field,
			):
				raise ValueError("Invalid child ownership")
			if r["idx"] <= previous or (child != "Context Case" and r["idx"] != idx):
				raise ValueError("Invalid child order")
			previous = r["idx"]
		row[field] = [worker.mapped_row(db, cache, child, r, child=True) for r in records]
	doc = frappe.get_doc({"doctype": dt, **row})
	prepare_insert(doc, raw["name"])
	return doc


def write_docs(docs):
	groups = defaultdict(list)
	for doc in docs:
		values = doc.get_valid_dict(convert_dates_to_str=True, ignore_virtual=True)
		groups[doc.doctype, tuple(values)].append(tuple(values.values()))
	for (dt, fields), values in groups.items():
		started = monotonic()
		frappe.db.bulk_insert(dt, list(fields), values)
		TIMES["bulk_sql_seconds"] += monotonic() - started
		ROWS[dt] += len(values)
	for doc in docs:
		doc.set("__islocal", False)


def finish(db, item, doc):
	finish_insert(doc)
	if doc.doctype == "Person" and item["files"]:
		(attachment,) = item["files"]
		(data,) = db.execute("select bytes from blobs where sha=?", (attachment["sha256"],)).fetchone()
		doc.person_picture = attach_picture_bytes(doc, attachment["row"], data)
		doc.save()
		worker.restore_audit("File", attachment["row"])
	for child, rows in item["children"].items():
		for row in rows:
			worker.restore_audit(child, row)
	worker.restore_audit(doc.doctype, item["row"])


def write_batch(db, context, items, sequence):
	from ucm.ucm.doctype.activity_detail.activity_detail import BY, LOCKED, ON

	assert frappe.local.site == SITES["sql"]
	dt = items[0]["doctype"]
	if dt not in ORDER or dt in ("ToDo", "Record Share") or any(i["doctype"] != dt for i in items):
		raise ValueError("Expected one business document type per batch")
	cache = {}
	buffered = []

	def flush():
		if buffered:
			write_docs([doc for _, doc in buffered])
			write_docs([child for _, doc in buffered for child in doc.get_all_children()])
			for item, doc in buffered:
				finish(db, item, doc)
			buffered.clear()

	for item in items:
		raw = item["row"]
		# Preserve UCM's explicit tag authorization and historical lock command.
		if dt == "Record Tag" or (dt == "Activity Detail" and (raw[LOCKED] != 0 or raw[BY] or raw[ON])):
			flush()
			worker.insert_item(db, cache, context, item)
			ROWS["ORM " + dt] += 1
		else:
			buffered.append((item, prepare(db, cache, context, item)))
	flush()
