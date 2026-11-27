"""Undo a hidden publication in reverse receipt order, preserving the run audit."""

import json
import os
import tempfile
from pathlib import Path
from time import monotonic

import frappe

from . import api, publication_index
from .application import check_closed, lock_run
from .frozen import ORDER
from .publication_receiver import indexed_publication, remove_after_discard
from .publication_worker import audit_values
from .stored_rows import checked_row


def restore_missing(path, content):
	"""A worker can be killed between Frappe unlinking a picture and MariaDB commit."""
	if path.exists():
		return
	with tempfile.TemporaryDirectory(dir=path.parent) as work:
		temp = Path(work) / "picture"
		with temp.open("xb") as out:
			os.fchmod(out.fileno(), 0o600)
			out.write(content)
			out.flush()
			os.fsync(out.fileno())
		try:
			os.link(temp, path)
		except FileExistsError:
			pass


def delete_record(dt, name):
	from frappe.model.delete_doc import delete_doc

	# Complete Frappe's related-record cleanup in this transaction. An old queued
	# delete_dynamic_links job could otherwise erase assignments after re-import.
	delete_doc(
		dt,
		name,
		delete_permanently=True,
		ignore_missing=False,
		delete_dynamic_links_now=True,
	)


def remove_item(db, context, item):
	from hashlib import file_digest

	from ucm.ucm.doctype.activity_detail.activity_detail import LOCKED, set_locked

	dt, raw = item["doctype"], item["row"]
	if dt not in ORDER or dt in ("ToDo", "Record Share") or raw["context_tech_id"] != context:
		raise ValueError("Unexpected cleanup document")
	checked_row(dt, {**audit_values(raw), "context_tech_id": context})
	# Frappe cascades all child tables and files. Reject anything beyond this run's inventory.
	children = item["children"]
	for field in frappe.get_meta(dt).get_table_fields():
		expected = [r["name"] for r in children.get(field.options, []) if r["parentfield"] == field.fieldname]
		stored = frappe.db.get_values(
			field.options,
			filters={
				"parenttype": dt,
				"parent": raw["name"],
				"parentfield": field.fieldname,
			},
			order_by="idx asc",
			pluck=True,
			for_update=True,
		)
		if [str(name) for name in stored] != expected:
			raise ValueError("Child inventory changed after import")
	files = item.get("files", [])
	stored_files = frappe.db.get_values(
		"File",
		filters={"attached_to_doctype": dt, "attached_to_name": raw["name"]},
		pluck=True,
		for_update=True,
	)
	if set(stored_files) != {f["row"]["name"] for f in files}:
		raise ValueError("Attachment inventory changed after import")
	for f in files:
		metadata = f["row"]
		checked_row(
			"File",
			{
				**audit_values(metadata),
				"is_private": 1,
				"content_hash": metadata["content_hash"],
				"attached_to_field": "person_picture",
			},
		)
		file = frappe.get_doc("File", metadata["name"])
		path = Path(file.get_full_path()).resolve()
		if path.parent != Path(frappe.get_site_path("private", "files")).resolve():
			raise ValueError("Unexpected picture location")
		(content,) = db.execute("select bytes from blobs where sha=?", (f["sha256"],)).fetchone()
		restore_missing(path, content)
		with path.open("rb") as handle:
			if file_digest(handle, "sha256").hexdigest() != f["sha256"]:
				raise ValueError("Attachment changed after import")
		frappe.db.after_rollback.add(lambda path=path, content=content: restore_missing(path, content))
		delete_record("File", file.name)
	if dt == "Activity Detail" and raw[LOCKED]:
		set_locked(raw["name"], 0)
	delete_record(dt, raw["name"])


def discard(run: str, max_batches: int = 1, *, ticket=None, stop_at=None, deadline=None):
	"""Remove committed batches in reverse, one transaction each. A queued job passes a
	monotonic `deadline`: no batch is started after it, and the next job continues."""
	api._operator()
	if int(max_batches) < 1:
		raise ValueError("Select a positive batch limit")
	ctx, record = lock_run(run)
	if record.state == "Discarded":
		remove_after_discard(record)
		return api._result(record)
	if record.state not in (
		"Publication staged",
		"Publication applying",
		"Publication paused",
		"Publication ready",
		"Publication cleaning",
	):
		raise ValueError("Only a hidden full publication can be discarded")
	with indexed_publication(run, record) as (db, hashes, manifest, path, stamp):
		if (manifest["site"], manifest["context"], manifest) != (
			frappe.local.site,
			ctx.name,
			json.loads(record.manifest_json),
		):
			raise ValueError("Staged publication target differs")
		for _ in range(int(max_batches)):
			try:
				ctx, record = lock_run(run)
				if ticket is not None and record.dispatch_id != ticket:
					return api._result(record)
				if stop_at is not None and record.applied and record.applied <= stop_at:
					return api._result(record)
				if deadline is not None and monotonic() > deadline:
					return api._result(record)
				if record.state == "Discarded":
					return api._result(record)
				if record.state not in (
					"Publication staged",
					"Publication applying",
					"Publication paused",
					"Publication ready",
					"Publication cleaning",
				):
					raise ValueError("Publication is no longer eligible for discard")
				check_closed(ctx)
				sequence = record.applied or 0
				if sequence:
					sha, payload = publication_index.read_batch(db, hashes, sequence, manifest["batches"])
					batch = frappe.get_doc(api.BATCH, f"{run}-{sequence}", for_update=True)
					if (batch.digest, batch.sequence_number, batch.migration_run) != (
						sha,
						sequence,
						run,
					):
						raise ValueError("Cleanup receipt differs from publication")
					for item in reversed(json.loads(payload)):
						remove_item(db, ctx.name, item)
				record.applied = max(0, sequence - 1)
				record.state = "Publication cleaning" if record.applied else "Discarded"
				record.save(ignore_permissions=True)
				if not record.applied:
					remove_after_discard(record)
				publication_index.check_identity(path, stamp)
				frappe.db.commit()
				if not record.applied:
					break
			except Exception:
				frappe.db.rollback()
				raise
		return api._result(record)
