"""Commit sealed generator casework in restartable transactions on a rehearsal target."""

import hashlib
import json
from collections import Counter
from contextlib import contextmanager
from decimal import Decimal
from time import monotonic

import frappe
from frappe.utils import get_datetime, getdate, now_datetime

from . import (
	constants,
	held_shares,
	migration_load,
	publication_index,
	release_checkpoint,
	runs,
	sql_writer,
	write_routes,
)
from .activities import insert_activity
from .batch_metrics import collect, measure
from .dictionary_identity import resolve_term
from .frozen import (
	CHILDREN,
	LOOKUPS,
	ORDER,
	OWNERS,
	PLACE_ADDRESS_FIELDS,
	sensitive_policy,
)
from .history import insert_history
from .persons import attach_picture_bytes, insert_prepared_person
from .publication_receiver import (
	check_same_target,
	check_unchanged_site,
	indexed_publication,
)
from .publication_shares import check_share
from .runs import check_closed, lock_run
from .stored_rows import business, checked_row, insert_document


def mapped_row(db, cache, dt, raw, child=False):
	if set(raw) - set(frappe.db.get_table_columns(dt)):
		raise ValueError(f"Unexpected publication columns for {dt}")
	if raw.get("docstatus") != 0:
		raise ValueError(f"Unsupported document status for {dt}")
	row = business(raw, child)
	for field in frappe.get_meta(dt).fields:
		key = field.fieldname
		value = row.get(key)
		if value in (None, ""):
			continue
		if field.fieldtype == "Date":
			row[key] = getdate(value)
		if field.fieldtype == "Datetime":
			row[key] = get_datetime(value)
		if field.fieldtype in ("Float", "Currency", "Percent"):
			row[key] = Decimal(str(value))
		master = field.options
		if field.fieldtype != "Link" or master not in LOOKUPS:
			continue
		row[key] = resolve_reference(db, cache, master, value)
	return row


def references(dt, raw):
	"""The references of a published row that are resolved on the site: the master and the published key of
	each link to a DocType the publication describes."""
	row = business(raw)
	found = [
		(field.options, row[field.fieldname])
		for field in frappe.get_meta(dt).fields
		if field.fieldtype == "Link"
		and field.options in LOOKUPS
		and row.get(field.fieldname) not in (None, "")
	]
	return list(dict.fromkeys(found))


def reference_attributes(db, master, value):
	"""What the publication says of a reference: the attributes its record on the site must have."""
	ref = db.execute("select payload from refs where dt=? and id=?", (master, value)).fetchone()
	if not ref:
		raise ValueError(f"Missing reference for {master}")
	return json.loads(ref[0])


def resolve_attributes(cache, master, value, attributes):
	"""Find on the site the record a publication refers to, by the attributes published with it."""
	fields = LOOKUPS[master]
	if master == "Concept Value":
		resolved, _ = resolve_term(frappe.db._conn, value, [attributes[k] for k in fields])
	else:
		resolved = value
		if not frappe.db.exists(master, value):
			matches = frappe.get_all(
				master,
				filters={fields[0]: attributes[fields[0]]},
				pluck="name",
				limit=2,
			)
			resolved = matches[0] if len(matches) == 1 else None
		if resolved and dict(frappe.db.get_value(master, resolved, list(fields), as_dict=True)) != attributes:
			resolved = None
	if not resolved:
		raise runs.Unprepared(
			f"A {master} the delegation refers to is not on this site, or differs from the published one"
		)
	cache[(master, value)] = resolved
	return resolved


def resolve_reference(db, cache, master, value):
	"""`db` is the sealed file, or None where the cache already holds every reference of the row: a held
	share is released from what its run kept, after the file is gone."""
	if (master, value) not in cache:
		if db is None:
			raise ValueError(f"Missing reference for {master}")
		resolve_attributes(cache, master, value, reference_attributes(db, master, value))
	return cache[(master, value)]


def restore_audit(dt, raw, verified_actors=None):
	audit = {k: raw[k] for k in ("creation", "modified", "owner", "modified_by", "idx")}
	for k in ("creation", "modified"):
		audit[k] = get_datetime(audit[k])
	if verified_actors is None:
		verified_actors = set()
	for actor in (audit["owner"], audit["modified_by"]):
		if actor not in verified_actors:
			if not actor or not frappe.db.exists("User", actor):
				raise ValueError(f"Unresolved historical actor for {dt}")
			verified_actors.add(actor)
	frappe.db.set_value(dt, raw["name"], audit, update_modified=False)
	checked_row(dt, {"name": raw["name"], **audit})


def verify_place_address(db, cache, key):
	if not key or ("Place Address", key) in cache:
		return
	ref = db.execute("select payload from refs where dt='Place Address' and id=?", (key,)).fetchone()
	if ref is None:
		if json.loads(db.execute("select manifest from descriptor").fetchone()[0]).get(
			"place_addresses_sealed"
		):
			raise ValueError("Missing published place address projection")
		return  # A previously staged publication used the pre-provisioned geography.
	expected = json.loads(ref[0])
	rows = frappe.get_all(
		"Place Address",
		filters={
			"parent": key,
			"parenttype": "Place",
			"parentfield": "place_addresses",
		},
		fields=list(PLACE_ADDRESS_FIELDS),
		order_by="idx asc, name asc",
		limit=1,
	)
	actual = dict(rows[0]) if rows else None
	# apply_place numbers a sole generated address from one, as Frappe does.
	if (
		expected
		and actual
		and expected["idx"] == 0
		and actual["idx"] == 1
		and frappe.db.count(
			"Place Address",
			{"parent": key, "parenttype": "Place", "parentfield": "place_addresses"},
		)
		== 1
	):
		expected["idx"] = 1
	if actual != expected:
		raise ValueError("Target place address differs from the sealed publication")
	cache["Place Address", key] = True


def insert_tag(row, context, insert=insert_document):
	from ucm.permissions import is_unrestricted
	from ucm.permissions.sensitivity import _record, _tag_usable, cleared, refresh_tags

	dt, key, tag = row["tag_doctype"], row["tag_record"], row["sensitivity_tag_tech_id"]
	parent = _record(dt, key, write=True)
	if parent.context_tech_id != context:
		raise ValueError("Published tag refers outside the incoming delegation")
	_tag_usable(tag, context)
	if not is_unrestricted() and tag not in cleared(context, write=True):
		raise frappe.PermissionError("Operator lacks write clearance for this sensitivity tag")
	# UCM writes Record Tag only after authorizing the parent, context and clearance.
	# Preserve the original tag key and actor without changing an existing live tag.
	insert(
		frappe.get_doc({"doctype": "Record Tag", **row}),
		set_name=row["name"],
		ignore_permissions=True,
	)
	refresh_tags(dt, key)


@measure("validation")
def prepare_item_rows(db, cache, context, item):
	dt = item["doctype"]
	if dt not in ORDER or dt in constants.DEFERRED:
		raise ValueError("Unsupported business batch")
	raw = item["row"]
	if raw["context_tech_id"] != context:
		raise ValueError(f"Delegation differs for {dt}")
	if dt in OWNERS:
		owner, field = OWNERS[dt]
		if frappe.db.get_value(owner, raw[field], "context_tech_id") != context:
			raise ValueError(f"Parent delegation differs for {dt}")
	row = mapped_row(db, cache, dt, raw)
	nested = {}
	if set(item["children"]) != set(CHILDREN.get(dt, ())):
		raise ValueError(f"Unexpected child tables for {dt}")
	for child, children in item["children"].items():
		field = next(
			f.fieldname for f in frappe.get_meta(dt).fields if f.fieldtype == "Table" and f.options == child
		)
		nested[field] = []
		previous = 0
		for idx, record in enumerate(children, 1):
			# The generator retains the receiver's position in a Context Case list
			# when an intervening receiver declined the share. Its ordered keys are stable.
			position = record["idx"]
			if (record["parent"], record["parenttype"], record["parentfield"]) != (
				row["name"],
				dt,
				field,
			) or (position <= previous if child == "Context Case" else position != idx):
				raise ValueError(f"Invalid {child} ownership or order")
			previous = position
			nested[field].append(mapped_row(db, cache, child, record, child=True))
	return row, nested


def picture_attacher(db, cache, file_sha, insert):
	def attach(doc, metadata):
		(content,) = db.execute("select bytes from blobs where sha=?", (file_sha,)).fetchone()
		if hashlib.sha256(content).hexdigest() != file_sha:
			raise ValueError("Private picture bytes differ from approved batch")
		# Use File.set_folder_name's target lookup once in this transaction.
		if "attachments_folder" not in cache:
			cache["attachments_folder"] = frappe.db.get_value("File", {"is_attachments_folder": 1})
		return attach_picture_bytes(
			doc,
			metadata,
			content,
			folder=cache["attachments_folder"],
			insert=insert,
		)

	return attach


def insert_item(db, cache, context, item, verified_actors=None, *, insert=insert_document):
	dt, raw = item["doctype"], item["row"]
	row, nested = prepare_item_rows(db, cache, context, item)
	if dt == "Person":
		files = item["files"]
		attach = picture_attacher(db, cache, files[0]["sha256"] if files else None, insert)
		insert_prepared_person(row, nested, [f["row"] for f in files], attach, insert=insert)
		for f in files:
			restore_audit("File", f["row"], verified_actors)
	elif dt == "Activity":
		verify_place_address(db, cache, row["activity_place_tech_id"])
		insert_activity(
			row,
			nested["activity_objectives"],
			{},
			address_from_target=True,
			insert=insert,
		)
	else:
		if dt == "Activity Detail":
			insert_history(row, insert=insert)
			# UCM recomputes the flag as subsequent historical rows arrive.
			row.pop("activity_detail_latest_state")
		elif dt == "Record Tag":
			insert_tag(row, context, insert=insert)
		else:
			doc = frappe.get_doc(
				{
					"doctype": dt,
					**row,
					**{k: [dict(r) for r in v] for k, v in nested.items()},
				}
			)
			insert(doc, set_name=row["name"], set_child_names=False)
		# Record Tag batches populate inherited sensitivity fields later in this wave.
		# Activation compares every published field once all tags are present.
		if dt in ("Activity Note", "Activity Detail", "Activity Detail Note"):
			row.pop("activity_tags", None)
			row.pop("person_tags", None)
		checked_row(dt, row)
		for field, children in nested.items():
			child_type = frappe.get_meta(dt).get_field(field).options
			saved = frappe.get_doc(dt, row["name"]).get(field)
			if [str(r.name) for r in saved] != [r["name"] for r in children]:
				raise ValueError(f"Stored {child_type} order differs")
			for child in children:
				if child_type == "Context Case":
					frappe.db.set_value(
						child_type,
						child["name"],
						"idx",
						child["idx"],
						update_modified=False,
					)
				checked_row(child_type, child)
	for child, children in item["children"].items():
		for record in children:
			restore_audit(child, record, verified_actors)
	restore_audit(dt, raw, verified_actors)


def insert_batch(db, context, items, sequence, *, insert=insert_document):
	cache = {}
	verified_actors = set()
	for position, item in enumerate(items, 1):
		try:
			insert_item(db, cache, context, item, verified_actors, insert=insert)
		except Exception as exc:
			exc.migration_location = (sequence, position, item["doctype"])
			raise


def apply(run: str, max_batches: int = 1, *, ticket=None, stop_at=None, deadline=None):
	"""Commit business batches one transaction each, then reconcile. A queued job passes a
	monotonic `deadline`: no batch is started after it, and the next job continues."""
	runs.only_admin()
	if int(max_batches) < 1:
		raise ValueError("Select a positive batch limit")
	_, initial = lock_run(run)
	refuse_set_based(initial)
	with indexed_publication(run, initial) as (db, hashes, manifest, path, stamp):
		check_unchanged_site(manifest)
		remaining = int(max_batches)
		while remaining:
			try:
				ctx, record = lock_run(run)
				if ticket is not None and record.dispatch_id != ticket:
					return runs.result(record)
				if stop_at is not None and (record.applied or 0) >= stop_at:
					return runs.result(record)
				if deadline is not None and monotonic() > deadline:
					return runs.result(record)
				check_same_target(manifest, ctx.name, record)
				if record.state == constants.READY:
					return {
						**runs.result(record),
						"pending_activation_from_batch": (record.applied or 0) + 1,
					}
				if record.state not in (
					constants.STAGED,
					constants.APPLYING,
				):
					raise ValueError("Run cannot apply a business batch")
				if reason := migration_load.pressure():
					record.update(
						{
							"state": constants.PAUSED,
							"finding": reason,
							"worker_phase": "Paused for live load",
							"worker_seen": now_datetime(),
						}
					)
					record.save(ignore_permissions=True)
					frappe.db.commit()
					return runs.result(record)
				publication_index.check_identity(path, stamp)
				started = monotonic()
				check_closed(ctx)
				if not record.applied:
					from .publication_prerequisites import verify_links

					verify_sensitive_policy(manifest)
					verify_links(db)
				sequence = int(record.applied or 0) + 1
				data = publication_index.read_batch(db, hashes, sequence, manifest["batches"])
				items = json.loads(data[1]) if data else []
				if not items or items[0]["doctype"] in constants.DEFERRED:
					complete = reconcile_business(db, ctx, record, manifest, checkpoint=True)
					publication_index.check_identity(path, stamp)
					if not complete:
						record.state = constants.APPLYING
						record.save(ignore_permissions=True)
						frappe.db.commit()
						if ticket is not None:
							return runs.result(record)
						continue
					record.state = constants.READY
					record.worker_phase = "Ready for activation"
					record.save(ignore_permissions=True)
					frappe.db.commit()
					return {
						**runs.result(record),
						"pending_activation_from_batch": sequence,
					}
				sha = data[0]
				dt = items[0]["doctype"]
				if any(item["doctype"] != dt for item in items):
					raise ValueError("Expected one parent DocType per publication batch")
				plan = json.loads(record.write_plan_json or "{}")
				route = write_routes.route_for(plan, dt)
				writer = sql_writer.write_batch if route == write_routes.SQL else insert_batch
				writing = monotonic()
				with collect() as metrics:
					writer(db, ctx.name, items, sequence)
				duration = monotonic() - writing
				publication_index.check_identity(path, stamp)
				frappe.get_doc(
					{
						"doctype": constants.BATCH,
						"migration_run": run,
						"sequence_number": sequence,
						"write_route": route,
						"parent_doctype": dt,
						"digest": sha,
						"parent_rows": len(items),
						"payload_bytes": len(data[1]),
						"attachment_bytes": sum(
							f["row"]["file_size"] for item in items for f in item.get("files", [])
						),
						"verify_seconds": writing - started,
						"write_seconds": duration,
						"validation_seconds": metrics["validation"],
						"attachment_seconds": metrics["attachments"],
					}
				).insert(set_name=f"{run}-{sequence}", ignore_permissions=True)
				remaining -= 1
				record.update(
					{
						"state": constants.APPLYING,
						"applied": sequence,
						"worker_phase": "Writing " + dt,
						"worker_seen": now_datetime(),
					}
				)
				record.save(ignore_permissions=True)
				frappe.db.commit()
			except Exception:
				frappe.db.rollback()
				raise
		return runs.result(record)


def audit_values(raw):
	return {
		"name": raw["name"],
		"creation": get_datetime(raw["creation"]),
		"modified": get_datetime(raw["modified"]),
		**{k: raw[k] for k in ("owner", "modified_by", "idx", "docstatus")},
	}


def reconcile_item(db, cache, item):
	from hashlib import file_digest
	from pathlib import Path

	from ucm.controllers.document import ContextDocument
	from ucm.controllers.validation import validate_option_sets, validate_same_context

	from .activities import check_activity

	dt, raw = item["doctype"], item["row"]
	row = mapped_row(db, cache, dt, raw)
	current = frappe.get_doc(dt, raw["name"], for_update=True)
	validate_option_sets(current)
	if isinstance(current, ContextDocument):
		validate_same_context(current)
	if dt == "Person":
		stored_files = frappe.db.get_values(
			"File",
			{"attached_to_doctype": dt, "attached_to_name": raw["name"]},
			pluck=True,
			for_update=True,
		)
		if set(stored_files) != {f["row"]["name"] for f in item["files"]}:
			raise ValueError("Stored person attachment inventory differs")
	if dt == "Person" and item["files"]:
		(attachment,) = item["files"]
		metadata = attachment["row"]
		file = frappe.get_doc("File", metadata["name"], for_update=True)
		checked_row(
			"File",
			{
				**audit_values(metadata),
				**{
					k: metadata[k]
					for k in (
						"attached_to_doctype",
						"attached_to_name",
						"attached_to_field",
						"file_size",
						"content_hash",
						"is_private",
					)
				},
			},
		)
		with Path(file.get_full_path()).open("rb") as handle:
			if file_digest(handle, "sha256").hexdigest() != attachment["sha256"]:
				raise ValueError("Target picture differs from sealed bytes")
		row["person_picture"] = file.file_url
	if dt == "Activity":
		verify_place_address(db, cache, row["activity_place_tech_id"])
		check_activity(row, address_from_target=True)
	else:
		checked_row(dt, row)
	checked_row(dt, audit_values(raw))
	for child, records in item["children"].items():
		stored = frappe.get_all(
			child,
			filters={"parent": raw["name"], "parenttype": dt},
			order_by="idx asc",
			pluck="name",
		)
		if [str(name) for name in stored] != [r["name"] for r in records]:
			raise ValueError(f"Published {child} inventory differs")
		for child_row in records:
			checked_row(
				child,
				{
					**mapped_row(db, cache, child, child_row, child=True),
					**audit_values(child_row),
				},
			)


def release_item(db, cache, item, context, *, route=write_routes.DOCUMENT):
	dt, raw = item["doctype"], item["row"]
	if dt == "ToDo":
		master, key = raw["reference_type"], raw["reference_name"]
	elif dt == "Record Share" and raw["context_tech_id"] == context:
		master, key = raw["share_doctype"], raw["share_name"]
	else:
		raise ValueError("Unexpected activation document")
	if (
		master not in ("Person", "Activity", "Case")
		or frappe.db.get_value(master, key, "context_tech_id", for_update=True) != context
	):
		raise ValueError("Published access record refers outside this delegation")
	if dt == "ToDo" and not frappe.db.has_column(master, "_assign"):
		raise ValueError("Provision target assignment storage before activating")
	row = mapped_row(db, cache, dt, raw)
	if dt == "Record Share" and check_share(row, context) == "receiver_not_active":
		raise ValueError("A historical share to an inactive delegation must be held")
	# Record Share is a system-owned document. check_share above authorizes the
	# share command; its ORM controller still validates the published document.
	insert = sql_writer.insert_document if route == write_routes.SQL else insert_document
	insert(
		frappe.get_doc({"doctype": dt, **row}),
		set_name=row["name"],
		ignore_permissions=dt == "Record Share",
	)
	if dt == "ToDo" and not row["assigned_by_full_name"] and row["assigned_by"]:
		row["assigned_by_full_name"] = frappe.db.get_value("User", row["assigned_by"], "full_name")
	checked_row(dt, row)
	restore_audit(dt, raw)


def refresh_published_tags(db, context):
	from ucm.permissions.sensitivity import refresh_tags

	for (payload,) in db.execute("select payload from batches order by sequence"):
		items = json.loads(payload)
		if items[0]["doctype"] != "Record Tag":
			continue
		for item in items:
			raw = item["row"]
			dt, key = raw["tag_doctype"], raw["tag_record"]
			if (
				dt
				not in (
					"Activity",
					"Person",
					"Case",
					"Activity Note",
					"Person Note",
					"Activity Detail Note",
				)
				or frappe.db.get_value(dt, key, "context_tech_id", for_update=True) != context
			):
				raise ValueError("Published sensitivity tag refers outside this delegation")
			refresh_tags(dt, key)


def verify_sensitive_policy(manifest):
	if "Organization" in manifest["counts"] and sensitive_policy(
		frappe.db._conn, manifest["context"], for_update=True
	) != manifest.get("sensitive_field_policy"):
		raise runs.Unprepared(
			"The site's sensitive field assignments or the delegation's exemptions differ from the publication"
		)


def reconcile_business(db, ctx, record, manifest, *, checkpoint=False):
	from .frozen import selection

	verify_sensitive_policy(manifest)
	cursor = int(record.reconcile_cursor or 0) if checkpoint else 0
	if not cursor and manifest["counts"].get("Record Tag"):
		refresh_published_tags(db, ctx.name)
	cache = {}
	if not cursor:
		for dt in ORDER:
			if dt == "Organization" and dt not in manifest["counts"]:
				continue
			where = selection(dt, organizations="Organization" in manifest["counts"])
			# nosemgrep: frappe-semgrep-rules.rules.security.frappe-sql-format-injection -- reviewed: the DocType is one of ORDER and the condition is built by selection() from constants; the context is a parameter
			count = frappe.db.sql(
				f"select count(*) from `tab{dt}` r {where}",
				(ctx.name,) * where.count("%s"),
			)[0][0]
			expected = 0 if dt in constants.DEFERRED else manifest["counts"][dt]
			if count != expected:
				raise ValueError(f"Stored {dt} inventory differs before activation")
	checked = Counter(dict.fromkeys(manifest["counts"], 0))
	sensitive_by_owner = Counter()
	if cursor:
		previous = json.loads(record.reconcile_counts)
		checked.update(previous["checked"])
		sensitive_by_owner.update(previous["sensitive"])
	reused = 0
	slice_started = monotonic()
	boundary = min(cursor + 25, record.applied) if checkpoint else record.applied
	for seq, sha, payload in db.execute(
		"select sequence,sha,payload from batches where sequence>? and sequence<=? order by sequence",
		(cursor, boundary),
	):
		started = monotonic()
		receipt = frappe.get_doc(constants.BATCH, f"{record.name}-{seq}", for_update=True)
		if (receipt.migration_run, receipt.sequence_number, receipt.digest) != (
			record.name,
			seq,
			sha,
		) or hashlib.sha256(payload).hexdigest() != sha:
			raise ValueError("Committed receipt differs from publication")
		items = json.loads(payload)
		current_hash = (
			release_checkpoint.fingerprint(items, ctx.name)
			if checkpoint or receipt.release_fingerprint
			else None
		)
		unchanged = not checkpoint and current_hash and current_hash == receipt.release_fingerprint
		reused += int(bool(unchanged))
		for position, item in enumerate(items, 1):
			try:
				if not unchanged:
					reconcile_item(db, cache, item)
				checked[item["doctype"]] += 1
				if item["doctype"] == "Sensitive Information":
					sensitive_by_owner[item["row"]["record_doctype"]] += 1
				for child, rows in item["children"].items():
					checked[child] += len(rows)
				checked["File"] += len(item.get("files", []))
			except Exception as exc:
				exc.migration_location = (seq, position, item["doctype"])
				raise
		if checkpoint:
			frappe.db.set_value(
				constants.BATCH,
				receipt.name,
				{
					"reconcile_seconds": monotonic() - started,
					"release_fingerprint": current_hash,
				},
			)
			if monotonic() - slice_started >= 10:
				boundary = seq
				break
	if checkpoint:
		record.update(
			{
				"reconcile_cursor": boundary,
				"reconcile_counts": json.dumps({"checked": checked, "sensitive": sensitive_by_owner}),
				"worker_seen": now_datetime(),
				"worker_phase": "Reconciling",
			}
		)
		if boundary < record.applied:
			return False
	deferred = {dt: manifest["counts"][dt] for dt in constants.DEFERRED}
	if any(checked[dt] + deferred.get(dt, 0) != count for dt, count in manifest["counts"].items()):
		raise ValueError("Verified rows and deferred access rows differ from the manifest")
	access = Counter()
	for (payload,) in db.execute(
		"select payload from batches where sequence>? order by sequence",
		(record.applied,),
	):
		items = json.loads(payload)
		if items[0]["doctype"] == "Record Share":
			for item in items:
				access[check_share(item["row"], ctx.name)] += 1
	record.reconciliation_json = json.dumps(
		{
			"context": ctx.name,
			"published_rows": manifest["counts"],
			"checked_business_rows": dict(checked),
			"sensitive_records_by_owner_type": dict(sensitive_by_owner),
			"deferred_access_rows": deferred,
			"batch_chain_sha256": manifest["batch_chain_sha256"],
			"committed_business_batches": record.applied,
			"unchanged_batches_verified_under_lock": reused,
			"deferred_batches": record.received - record.applied,
			"share_review": dict(access),
		}
	)
	return True


def refuse_set_based(record):
	"""The set-based route has its own steps: a run that chose it is not written through this worker."""
	if write_routes.set_based(json.loads(record.write_plan_json or "{}")):
		raise ValueError("This run uses the set-based route")


@contextmanager
def current_reads():
	"""Release checks of an activation, on either route, read the rows as they are now."""
	# MariaDB snapshot isolation rejects a locking read if a caseworker saved the
	# record since our reconciliation snapshot. Use InnoDB current locking reads
	# for release checks, retaining repeatable-read gap locks for absent consent.
	setting = frappe.db.sql("show session variables like 'innodb_snapshot_isolation'")
	restore = bool(setting and setting[0][1] == "ON")
	if restore:
		frappe.db.sql("set session innodb_snapshot_isolation=OFF")
	try:
		yield
	finally:
		if restore:
			frappe.db.sql("set session innodb_snapshot_isolation=ON")


def activate(run: str):
	runs.only_admin()
	with current_reads():
		return _activate(run)


def _activate(run):
	held_shares.lock_activation()
	ctx, record = lock_run(run)
	refuse_set_based(record)
	with indexed_publication(run, record) as (db, hashes, manifest, path, stamp):
		try:
			check_unchanged_site(manifest)
			check_same_target(manifest, ctx.name, record)
			if record.state == constants.ACTIVATED:
				return runs.result(record)
			if record.state not in (constants.READY, constants.APPLYING):
				raise ValueError("Run has no committed business batches")
			check_closed(ctx)
			reconcile_business(db, ctx, record, manifest)
			cache = {}
			frappe.flags.ucm_publishing_context = ctx.name
			held = 0
			released = Counter({"ToDo": 0, "Record Share": 0})
			for seq in range(record.applied + 1, manifest["batches"] + 1):
				sha, payload = publication_index.read_batch(db, hashes, seq, manifest["batches"])
				items = json.loads(payload)
				dt = items[0]["doctype"]
				if any(item["doctype"] != dt for item in items):
					raise ValueError("Mixed activation batch")
				route = write_routes.route_for(json.loads(record.write_plan_json or "{}"), dt)
				for position, item in enumerate(items, 1):
					try:
						if (
							item["doctype"] == "Record Share"
							and check_share(item["row"], ctx.name) == "receiver_not_active"
						):
							held_shares.hold(record, item["row"], seq, position, db)
							held += 1
							continue
						release_item(db, cache, item, ctx.name, route=route)
						released[item["doctype"]] += 1
					except Exception as exc:
						exc.migration_location = (seq, position, item["doctype"])
						raise
				frappe.get_doc(
					{
						"doctype": constants.BATCH,
						"migration_run": run,
						"sequence_number": seq,
						"write_route": route,
						"parent_doctype": items[0]["doctype"],
						"digest": sha,
					}
				).insert(set_name=f"{run}-{seq}", ignore_permissions=True)
			publication_index.check_identity(path, stamp)
			ctx.context_status = "Active"
			ctx.save()
			report = json.loads(record.reconciliation_json)
			report["inbound_share_outcomes"] = held_shares.release_incoming(ctx, run)
			report["shares_held_at_activation"] = held
			accounted = released.copy()
			accounted["Record Share"] += held
			if accounted != report["deferred_access_rows"]:
				raise ValueError("Release receipts differ from the deferred access inventory")
			report["released_access_rows"] = dict(released)
			frappe.db.set_value(
				constants.RUN,
				run,
				{
					"applied": manifest["batches"],
					"worker_phase": "Activated",
					"worker_seen": now_datetime(),
					"reconciliation_json": json.dumps(report),
				},
			)
			publication_index.check_identity(path, stamp)
			runs.mark_activated(run)
			result = runs.result(runs.record(run))
			# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: the activation is one transaction of the queued job, which ends here
			frappe.db.commit()
			return result
		except Exception:
			frappe.db.rollback()
			raise
		finally:
			frappe.flags.ucm_publishing_context = None
