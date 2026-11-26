"""The set-based SQL route for a sealed delegation publication.

The document route saves every published record through its controller: about a hundred records a second. This
route writes the published rows with multi-row INSERT statements and leaves the rules to four later steps, each
of which reads the delegation in sets:

	apply    the rows of each business batch, straight into UCM's tables, a transaction per group of batches
	derive   the values UCM derives, with UCM's own rebuilds, for this delegation only
	verify   stored rows against the publication, the integrity rules as queries, and a sample of records
	         through the document route's own check
	activate the assignments and shares held back, then the context, in one short transaction

`discard` removes a hidden delegation by primary key in chunks, and needs no change to Frappe.

No controller runs for a copied row, so every rule a save applies has to be named here: `integrity` lists the
ones checked as queries, and the module's tests compare a load by this route with the same load through the
documents. Sensitivity tags, assignments and shares stay on the document route: their rules read other
delegations, consent and clearances, and they are about one row in a hundred.
"""

import hashlib
import json
import os
import sqlite3
import tempfile
from collections import Counter, defaultdict
from contextlib import closing, contextmanager
from decimal import Decimal
from pathlib import Path
from time import monotonic

import frappe
from frappe import _
from frappe.utils import get_datetime, getdate, now_datetime

from ucm_migration_loader import api
from ucm_migration_loader.application import check_closed, lock_run
from ucm_migration_loader.frozen import CHILDREN, LOOKUPS, ORDER, selection, verify_connection
from ucm_migration_loader.publication_receiver import (
	local_publication,
	release_differences,
	remove_after_discard,
	schema_differences,
)

ROUTE = "Set-based SQL"
DOCUMENT = "Document API"
# Their rules read clearances, consent and other delegations: written through the documents on every route
THROUGH_DOCUMENTS = frozenset(("Record Tag",))
DEFERRED = ("ToDo", "Record Share")  # released at activation
STAGED, APPLYING, READY, CLEANING = (
	"Publication staged",
	"Publication applying",
	"Publication ready",
	"Publication cleaning",
)
HIDDEN = (STAGED, APPLYING, "Publication paused", READY, CLEANING)
INSERT_ROWS = 500  # rows per INSERT statement
COMMIT_ROWS = 5000  # parent rows per transaction: small enough for the replicas to follow
DELETE_ROWS = 5000
NUMERIC = ("Int", "Float", "Currency", "Percent", "Check", "Rating", "Duration")
NO_COLUMN = ("Section Break", "Column Break", "Tab Break", "HTML", "Button", "Heading", "Fold")


@contextmanager
def _publication(run: str):
	"""The staged file, read only, in one snapshot."""
	uri = local_publication(run).resolve(strict=True).as_uri() + "?mode=ro"
	with closing(sqlite3.connect(uri, uri=True)) as db:
		db.execute("pragma trusted_schema=OFF")
		db.execute("begin")
		yield db


def _checked_manifest(db, run: str) -> dict:
	manifest = verify_connection(db, run)
	if release_differences(manifest):
		raise ValueError(_("Target application versions differ from the reviewed publication"))
	if schema_differences(manifest):
		raise ValueError(_("Target schema differs from publication"))
	return manifest


def _same_target(manifest: dict, ctx, record) -> None:
	if (manifest["site"], manifest["context"], manifest) != (
		frappe.local.site,
		ctx.name,
		json.loads(record.manifest_json),
	):
		raise ValueError(_("Staged publication target differs"))


def _business_batches(db) -> int:
	"""The number of the last batch that is not an assignment or a share: those close the publication."""
	for sequence, payload in db.execute("select sequence, payload from batches order by sequence desc"):
		if json.loads(payload)[0]["doctype"] not in DEFERRED:
			return sequence
	return 0


# ---------------------------------------------------------------------------------------------------------------
# apply


def _plan(cache: dict, doctype: str) -> tuple:
	"""The table's columns, the conversions of its date fields and its links to shared reference data."""
	key = ("plan", doctype)
	if key not in cache:
		convert, lookups, blank = {"creation": get_datetime, "modified": get_datetime}, {}, []
		for df in frappe.get_meta(doctype).fields:
			if df.fieldtype == "Select" and (df.options or "").startswith("\n"):
				# a save stores a Select left empty as its first option: here the empty one, not NULL
				blank.append(df.fieldname)
			if df.fieldtype == "Date":
				convert[df.fieldname] = getdate
			elif df.fieldtype == "Datetime":
				convert[df.fieldname] = get_datetime
			elif df.fieldtype == "Link" and df.options in LOOKUPS:
				lookups[df.fieldname] = df.options
		cache[key] = frozenset(frappe.db.get_table_columns(doctype)), convert, lookups, blank
	return cache[key]


def _actor(cache: dict, user: str) -> str:
	"""A published author is kept as published, and must be a user of this site."""
	known = cache.setdefault("actors", set())
	if user not in known:
		if not user or not frappe.db.exists("User", user):
			raise ValueError(_("Unresolved historical actor"))
		known.add(user)
	return user


def stored_row(db, cache: dict, doctype: str, raw: dict) -> dict:
	"""A published row as its table stores it: every published column, dates as dates, and links to the shared
	dictionaries and catalogs as this site's own keys."""
	from ucm_migration_loader.publication_worker import resolve_reference

	columns, convert, lookups, blank = _plan(cache, doctype)
	if set(raw) - columns:
		raise ValueError(_("Unexpected publication columns for {0}").format(doctype))
	if raw.get("docstatus") != 0:
		raise ValueError(_("Unsupported document status for {0}").format(doctype))
	row = dict(raw)
	for field, to_stored in convert.items():
		if field in row:
			row[field] = to_stored(row[field]) if row[field] not in (None, "") else None
	for field in blank:
		if field in row and row[field] is None:
			row[field] = ""
	for field, master in lookups.items():
		if row.get(field) not in (None, ""):
			row[field] = resolve_reference(db, cache, master, row[field])
	for field in ("owner", "modified_by"):
		row[field] = _actor(cache, row[field])
	return row


def _insert(doctype: str, rows: list[dict]) -> None:
	if not rows:
		return
	fields = list(rows[0])
	if any(row.keys() != rows[0].keys() for row in rows):
		raise ValueError(_("Inconsistent published columns for {0}").format(doctype))
	columns = ", ".join(f"`{field}`" for field in fields)
	one = "(" + ", ".join(["%s"] * len(fields)) + ")"
	for start in range(0, len(rows), INSERT_ROWS):
		chunk = rows[start : start + INSERT_ROWS]
		# nosemgrep: frappe-semgrep-rules.rules.security.frappe-sql-format-injection -- reviewed: the table is a DocType of the publication profile and the columns were checked against the table's own (stored_row); every value is a parameter
		frappe.db.sql(
			f"insert into `tab{doctype}` ({columns}) values " + ", ".join([one] * len(chunk)),
			[row[field] for row in chunk for field in fields],
		)


def _picture(db, cache: dict, person: dict, metadata: dict, sha: str) -> None:
	"""Put a person's private picture where its File row says, once per distinct content."""
	from frappe.core.doctype.file.utils import get_content_hash

	url = metadata["file_url"] or ""
	if (
		metadata["is_private"] != 1
		or not url.startswith("/private/files/")
		or (metadata["attached_to_doctype"], metadata["attached_to_name"], metadata["attached_to_field"])
		!= ("Person", person["name"], "person_picture")
	):
		raise ValueError(_("Invalid private person picture metadata"))
	facts = cache.setdefault("pictures", {})
	if sha not in facts:
		(content,) = db.execute("select bytes from blobs where sha=?", (sha,)).fetchone()
		facts[sha] = len(content), get_content_hash(content)
	if facts[sha] != (metadata["file_size"], metadata["content_hash"]) or not facts[sha][0]:
		raise ValueError(_("Source private picture differs from its File metadata"))
	placed = cache.setdefault("placed", set())
	if (sha, url) in placed:
		return
	root = Path(frappe.get_site_path("private", "files")).resolve()
	path = (root / url.removeprefix("/private/files/")).resolve()
	if path.parent != root:
		raise ValueError(_("Unexpected picture location"))
	if path.exists():
		with path.open("rb") as handle:
			if hashlib.file_digest(handle, "sha256").hexdigest() != sha:
				# Frappe would store the picture under another name: the copied rows cannot follow it there
				raise ValueError(_("Another file already has the name of a published picture"))
	else:
		(content,) = db.execute("select bytes from blobs where sha=?", (sha,)).fetchone()
		with tempfile.TemporaryDirectory(dir=root) as work:
			temporary = Path(work) / "picture"
			with temporary.open("xb") as out:
				os.fchmod(out.fileno(), 0o600)
				out.write(content)
				out.flush()
				os.fsync(out.fileno())
			try:
				os.link(temporary, path)
			except FileExistsError:
				pass
	placed.add((sha, url))


def _item_rows(db, cache: dict, context: str, doctype: str, item: dict, rows: dict) -> None:
	"""Add a published record, its child rows and its picture's File row to the rows to write."""
	raw = item["row"]
	if item["doctype"] != doctype:
		raise ValueError(_("Expected one parent DocType per publication batch"))
	if raw["context_tech_id"] != context:
		raise ValueError(_("Delegation differs for {0}").format(doctype))
	if set(item["children"]) != set(CHILDREN.get(doctype, ())):
		raise ValueError(_("Unexpected child tables for {0}").format(doctype))
	files = item.get("files", [])
	if doctype == "Person":
		url = raw["person_picture"]
		if len(files) != int(bool(url)) or (files and files[0]["row"]["file_url"] != url):
			raise ValueError(_("Published picture and file inventory differ"))
	elif files:
		raise ValueError(_("Unexpected attachment for {0}").format(doctype))
	rows[doctype].append(stored_row(db, cache, doctype, raw))
	for child, children in item["children"].items():
		for child_row in children:
			if (child_row["parent"], child_row["parenttype"]) != (raw["name"], doctype):
				raise ValueError(_("Invalid {0} ownership").format(child))
			rows[child].append(stored_row(db, cache, child, child_row))
	for attachment in files:
		_picture(db, cache, raw, attachment["row"], attachment["sha256"])
		rows["File"].append(stored_row(db, cache, "File", attachment["row"]))


def _group_rows(db, cache: dict, context: str, doctype: str, group: list) -> dict[str, list[dict]]:
	"""The rows of a group of batches, per table, parents first."""
	rows = defaultdict(list)
	for sequence, _sha, items in group:
		for position, item in enumerate(items, 1):
			try:
				_item_rows(db, cache, context, doctype, item, rows)
			except Exception as exc:
				exc.migration_location = (sequence, position, doctype)
				raise
	return rows


def _receipts(record, group: list, doctype: str, route: str) -> None:
	"""A receipt per batch, as the document route leaves: written in one statement, the loader's own DocType."""
	from ucm_migration_loader.display_ids import receipt_id

	now, user = now_datetime(), frappe.session.user
	fields = [
		"name",
		"creation",
		"modified",
		"owner",
		"modified_by",
		"docstatus",
		"idx",
		"display_id",
		"migration_run",
		"sequence_number",
		"digest",
		"write_route",
		"parent_doctype",
	]
	frappe.db.bulk_insert(
		api.BATCH,
		fields,
		(
			(
				f"{record.name}-{sequence}",
				now,
				now,
				user,
				user,
				0,
				0,
				receipt_id(record.name, sequence),
				record.name,
				sequence,
				sha,
				route,
				doctype,
			)
			for sequence, sha, _items in group
		),
	)


def _groups(db, start: int, stop_at: int | None):
	"""Consecutive batches of one DocType, up to COMMIT_ROWS parents: what one transaction writes."""
	group, doctype, size = [], None, 0
	for sequence, sha, payload in db.execute(
		"select sequence, sha, payload from batches where sequence > ? order by sequence", (start,)
	):
		if stop_at is not None and sequence > stop_at:
			break
		items = json.loads(payload)
		kind = items[0]["doctype"]
		if kind in DEFERRED:
			break
		if kind not in ORDER:
			raise ValueError(_("Unsupported business batch"))
		if group and (kind != doctype or size >= COMMIT_ROWS):
			yield doctype, group
			group, size = [], 0
		doctype = kind
		group.append((sequence, sha, items))
		size += len(items)
	if group:
		yield doctype, group


def apply(run: str, stop_at: int | None = None) -> dict:
	"""Write the business batches not yet applied, from the last committed one on. Each group of batches, its
	receipts and the run's cursor commit together, so an interrupted apply resumes where it stopped."""
	from ucm.permissions import counts

	from ucm_migration_loader import publication_worker as documents
	from ucm_migration_loader.publication_prerequisites import verify_links

	api._operator()
	written, seconds, started = Counter(), Counter(), monotonic()
	with _publication(run) as db:
		manifest = _checked_manifest(db, run)
		seconds["check the sealed file"] = monotonic() - started
		cache = {}
		_ctx, record = lock_run(run)
		start = record.applied or 0
		frappe.db.rollback()
		for doctype, group in _groups(db, start, stop_at):
			try:
				ctx, record = lock_run(run)
				_same_target(manifest, ctx, record)
				if record.state not in (STAGED, APPLYING) or (record.applied or 0) != group[0][0] - 1:
					frappe.db.rollback()
					break  # paused, superseded or moved by another worker
				check_closed(ctx, pilot=False)
				if not record.applied:
					step = monotonic()
					documents.verify_sensitive_policy(manifest)
					verify_links(db)
					seconds["resolve every reference"] = monotonic() - step
				step = monotonic()
				if doctype in THROUGH_DOCUMENTS:
					for sequence, _sha, items in group:
						documents.insert_batch(db, ctx.name, items, sequence)
					route = DOCUMENT
					written[doctype] += sum(len(items) for _s, _h, items in group)
				else:
					rows = _group_rows(db, cache, ctx.name, doctype, group)
					seconds["of which reading the file and preparing rows"] += monotonic() - step
					names = tuple(row["name"] for row in rows[doctype])
					# the counts of the home move with the records, in the same transaction (ucm.permissions.counts)
					with counts.moving(doctype, "x.name in %(names)s", {"names": names}):
						for table, table_rows in rows.items():
							_insert(table, table_rows)
							written[table] += len(table_rows)
					route = ROUTE
				_receipts(record, group, doctype, route)
				frappe.db.set_value(
					api.RUN, run, {"state": APPLYING, "applied": group[-1][0], "finding": None}
				)
				# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: a load in small transactions by design: the rows, their receipts and the cursor commit together, and the replicas follow
				frappe.db.commit()
				seconds["write " + doctype] += monotonic() - step
			except Exception:
				frappe.db.rollback()
				raise
	return {
		**api._result(api._record(run)),
		"rows_written": dict(written),
		"seconds": {step: round(value, 2) for step, value in seconds.items()},
		"total_seconds": round(monotonic() - started, 2),
	}


# ---------------------------------------------------------------------------------------------------------------
# derive


def _marks(context: str) -> tuple:
	return frappe.db.sql(
		"""select count(*), coalesce(sum(crc32(name)), 0) from `tabActivity Detail`
		where context_tech_id = %s and activity_detail_latest_state = 1""",
		context,
	)[0]


def _stale(carrier: str, field: str, context: str) -> int:
	"""History rows or notes of this delegation's activities whose copy of an activity's column differs."""
	# nosemgrep: frappe-semgrep-rules.rules.security.frappe-sql-format-injection -- reviewed: carrier and field are UCM's constants (ACTIVITY_CARRIERS, FIELD, COUNTRY); the context is a parameter
	return frappe.db.sql(
		f"""select count(*) from `tab{carrier}` d join `tabActivity` a on a.name = d.activity_tech_id
		where a.context_tech_id = %s and not (d.`{field}` <=> a.`{field}`)""",
		context,
	)[0][0]


def _complete_places(context: str) -> int:
	"""An activity's place type from its Place, and its country and areas from the Place's first address: what
	Activity.apply_place sets on a new activity. UCM has no set-based form of this rule yet, so it is written
	again here, on UCM's own field lists; rows that already hold these values are not written."""
	from ucm.ucm.doctype.activity.activity import ADDRESSES, PLACE_ADDRESS
	from ucm.utils import bulk

	pairs = list(zip(ADDRESSES[0], PLACE_ADDRESS, strict=True))
	scope, values = "context_tech_id = %(context)s", {"context": context}
	changed = bulk.update(
		"tabActivity",
		"a",
		"""update `tabActivity` a join `tabPlace` p on p.name = a.activity_place_tech_id
		set a.activity_place_type_tech_id = p.place_type_tech_id
		where {range} and a.context_tech_id = %(context)s
		and not (a.activity_place_type_tech_id <=> p.place_type_tech_id)""",
		values,
		where=scope,
	)
	assignments = ", ".join(f"a.`{field}` = pa.`{source}`" for field, source in pairs)
	differs = " and ".join(f"a.`{field}` <=> pa.`{source}`" for field, source in pairs)
	return changed + bulk.update(
		"tabActivity",
		"a",
		f"""update `tabActivity` a join `tabPlace Address` pa on pa.name = (
			select x.name from `tabPlace Address` x
			where x.parent = cast(a.activity_place_tech_id as char) and x.parenttype = 'Place'
			order by x.idx, x.name limit 1)
		set {assignments}
		where {{range}} and a.context_tech_id = %(context)s and not ({differs})""",
		values,
		where=scope,
	)


def derive(run: str) -> dict:
	"""The values a save derives, for this delegation's rows, with UCM's own set-based rebuilds. Returns the rows
	each one changed: none, where the publication already carried what UCM computes. A rebuild is called only
	where rows differ, and writes only those."""
	from ucm.ucm.doctype.activity.activity import COUNTRY, sync_all_countries
	from ucm.ucm.doctype.activity_detail.latest_state import BATCH, refresh_persons
	from ucm.ucm.doctype.protection_department.protection_department import (
		ACTIVITY_CARRIERS,
		FIELD,
		sync_all_details,
	)
	from ucm.ucm.doctype.record_share.record_share import rebuild_activity_facts

	api._operator()
	ctx, record = lock_run(run)
	if record.state != APPLYING:
		raise ValueError(_("Derive follows the last business batch of an applying run"))
	check_closed(ctx, pilot=False)
	context, started = ctx.name, monotonic()
	scope, values = "a.context_tech_id = %(context)s", {"context": context}
	changed = {"place geography": _complete_places(context)}
	stale = sum(_stale(carrier, FIELD, context) for carrier in ACTIVITY_CARRIERS)
	changed["department copies"] = sync_all_details(scope, values) if stale else 0
	stale = _stale("Activity Detail", COUNTRY, context)
	changed["country copies"] = sync_all_countries(scope, values) if stale else 0
	changed["share facts"] = rebuild_activity_facts(scope, values)
	before = _marks(context)
	persons = frappe.db.sql_list(
		"select distinct person_tech_id from `tabActivity Detail` where context_tech_id = %s", context
	)
	for start in range(0, len(persons), BATCH):
		refresh_persons(persons[start : start + BATCH])
	changed["latest state marks"] = int(_marks(context) != before)
	# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: a step of the load run from the bench or a worker, committed whole; UCM's rebuilds above commit per chunk themselves
	frappe.db.commit()
	return {"changed": changed, "seconds": round(monotonic() - started, 2)}


# ---------------------------------------------------------------------------------------------------------------
# verify


def _scope(doctype: str, context: str) -> tuple[str, dict]:
	"""FROM and WHERE of a table's rows of one delegation, on alias `r`, with its parameters."""
	for parent, children in CHILDREN.items():
		if doctype in children:
			return (
				f"from `tab{doctype}` r join `tab{parent}` p on p.name = r.parent"
				" where r.parenttype = %(parenttype)s and p.context_tech_id = %(context)s",
				{"parenttype": parent, "context": context},
			)
	if doctype == "File":
		return (
			"from `tabFile` r join `tabPerson` p on r.attached_to_name = cast(p.name as char)"
			" where r.attached_to_doctype = 'Person' and p.context_tech_id = %(context)s",
			{"context": context},
		)
	where = selection(doctype)
	return f"from `tab{doctype}` r " + where.replace("%s", "%(context)s"), {"context": context}


def _filled(field: str) -> str:
	return f"nullif(cast(r.`{field}` as char), '') is not null"


def _link_rules(df, number: int, context: str, rules: dict, values: dict) -> None:
	"""The rules of a Link: its target exists, is a value of the field's option set and of the context's
	collection, and stays inside the context."""
	from ucm.ucm_dictionary.collections import allowed
	from ucm.utils.data_model import context_links, context_scopes, option_sets

	field, target = df.fieldname, frappe.get_meta(df.options)
	if target.issingle or target.is_virtual:
		return
	exists = f"exists (select 1 from `tab{df.options}` m where m.name = r.`{field}`"
	rules[f"{field}: link to nothing"] = f"{_filled(field)} and not {exists})"
	concept = dict(option_sets().get(df.parent, [])).get(field)
	if concept:
		values[f"concept{number}"] = concept
		rules[f"{field}: not an active value of its list"] = (
			f"{_filled(field)} and not {exists}"
			f" and m.concept_urn = %(concept{number})s and m.concept_value_usable = 1)"
		)
		offered = allowed(concept, context)
		if offered is not None:
			values[f"offered{number}"] = tuple(map(str, offered)) or ("",)
			rules[f"{field}: not in the list this context uses"] = (
				f"{_filled(field)} and cast(r.`{field}` as char) not in %(offered{number})s"
			)
	if field in dict(context_links().get(df.parent, [])):
		# global reference data (no context) may be linked from anywhere, where its DocType allows none
		orphan = "" if context_scopes().get(df.options) == "optional" else " or m.context_tech_id is null"
		rules[f"{field}: belongs to another context"] = (
			f"{_filled(field)} and {exists} and (m.context_tech_id <> %(context)s{orphan}))"
		)


def _record_rules(doctype: str, rules: dict, values: dict) -> None:
	"""The rules of one DocType's controller that read other tables in a way a query can."""
	if doctype == "Activity":
		# Activity.apply_place: the place its type allows, requires and types
		kind = "from `tabActivity Type` m where m.name = r.activity_type_tech_id"
		place = _filled("activity_place_tech_id")
		rules["place: its activity type records none"] = (
			f"{place} and not exists (select 1 {kind} and m.activity_type_enabled_place = 1)"
		)
		rules["place: required by its activity type"] = (
			f"not ({place}) and exists (select 1 {kind}"
			" and m.activity_type_enabled_place = 1 and m.activity_type_place_required = 1)"
		)
		rules["place: not of the place type its activity type asks for"] = (
			f"{place} and exists (select 1 {kind} and m.activity_type_place_type_tech_id is not null"
			" and not exists (select 1 from `tabPlace` p where p.name = r.activity_place_tech_id"
			" and p.place_type_tech_id = m.activity_type_place_type_tech_id))"
		)
		from ucm.ucm.doctype.activity.activity import ADDRESSES, PLACE_ADDRESS

		same = " and ".join(
			f"r.`{field}` <=> pa.`{source}`"
			for field, source in zip(ADDRESSES[0], PLACE_ADDRESS, strict=True)
		)
		rules["place: country or areas differ from the Place's first address"] = (
			f"{place} and exists (select 1 from `tabPlace Address` pa where pa.name = (select x.name"
			" from `tabPlace Address` x where x.parent = cast(r.activity_place_tech_id as char)"
			f" and x.parenttype = 'Place' order by x.idx, x.name limit 1) and not ({same}))"
		)
		rules["place: place type differs from the Place's"] = (
			f"{place} and not exists (select 1 from `tabPlace` p where p.name = r.activity_place_tech_id"
			" and p.place_type_tech_id <=> r.activity_place_type_tech_id)"
		)
	if doctype == "Activity Detail":
		from ucm.ucm.doctype.activity_detail.activity_detail import BY, LOCKED, ON

		values["now"] = now_datetime()
		rules["history lock: no known actor or a time to come"] = (
			f"r.`{LOCKED}` = 1 and (r.`{BY}` is null or r.`{ON}` is null or r.`{ON}` > %(now)s"
			f" or not exists (select 1 from `tabUser` m where m.name = r.`{BY}`))"
		)
		rules["history lock: actor or time without a lock"] = (
			f"ifnull(r.`{LOCKED}`, 0) = 0 and (r.`{BY}` is not null or r.`{ON}` is not null)"
		)


def _rules(doctype: str, context: str) -> tuple[dict[str, str], dict]:
	"""The rules of a table that a query can check, as {name: SQL counting the rows that break it}."""
	rules, values = {}, {}
	meta = frappe.get_meta(doctype)
	for number, df in enumerate(meta.fields):
		field = df.fieldname
		if df.fieldtype in NO_COLUMN or df.fieldtype in frappe.model.table_fields or df.is_virtual:
			continue
		if df.reqd and df.fieldtype not in NUMERIC and not df.mandatory_depends_on:
			rules[f"{field}: required"] = f"not ({_filled(field)})"
		default = (df.options or "").split("\n", 1)[0] if df.fieldtype == "Select" else df.default
		if (
			default
			and df.fieldtype not in NUMERIC
			and not str(default).startswith((":", "__", "Today", "Now"))
		):
			rules[f"{field}: empty where a save sets its default"] = f"not ({_filled(field)})"
		if df.fieldtype == "Select" and df.options:
			values[f"options{number}"] = tuple(o for o in df.options.split("\n") if o) or ("",)
			rules[f"{field}: not an option"] = f"{_filled(field)} and r.`{field}` not in %(options{number})s"
		if df.fieldtype == "Link" and df.options:
			_link_rules(df, number, context, rules, values)
	if meta.has_field("context_tech_id") and not meta.istable and doctype != "File":
		rules["context_tech_id: not this delegation"] = "not (r.context_tech_id <=> %(context)s)"
	_record_rules(doctype, rules, values)
	return rules, values


def integrity(context: str, doctypes: list[str]) -> dict:
	"""The rules a save applies that a query can check, counted over the delegation's rows: required fields,
	select options, links, option sets and collections, links that leave the context, history locks. One scan per
	table. Returns the rules broken, with their counts, and how many rules were checked."""
	broken, checked = {}, 0
	for doctype in doctypes:
		rules, values = _rules(doctype, context)
		if not rules:
			continue
		source, scope_values = _scope(doctype, context)
		sums = ", ".join(f"coalesce(sum({sql}), 0)" for sql in rules.values())
		# nosemgrep: frappe-semgrep-rules.rules.security.frappe-sql-format-injection -- reviewed: tables and fields come from the DocTypes' own definitions on this site (frappe.get_meta), the scope from frozen.selection; every value is a parameter
		counted = frappe.db.sql(f"select {sums} {source}", {**values, **scope_values})[0]
		checked += len(rules)
		found = {rule: int(count) for rule, count in zip(rules, counted, strict=True) if count}
		if found:
			broken[doctype] = found
	return {"rules_checked": checked, "broken": broken}


def _equal(stored, published) -> bool:
	if stored == published:
		return True
	if stored is None or published is None:
		return False
	# a number is published as it was read from the source: a decimal comes as text, 0E-9 for a zero
	if isinstance(stored, (int, float, Decimal)) or isinstance(published, (int, float, Decimal)):
		try:
			return Decimal(str(stored)) == Decimal(str(published))
		except ArithmeticError:
			return False
	return str(stored) == str(published)


def compare(db, context: str, applied: int) -> dict:
	"""Every stored column against the publication, read back a batch at a time. Returns the rows compared per
	table, and for the fields that differ how many rows and the first places (batch and item, never values)."""
	from ucm.ucm.doctype.activity.activity import ADDRESSES

	cache, compared, differing, places, missing = {}, Counter(), Counter(), {}, Counter()
	completed = 0
	for sequence, payload in db.execute(
		"select sequence, payload from batches where sequence <= ? order by sequence", (applied,)
	):
		expected = defaultdict(dict)
		where = {}
		for position, item in enumerate(json.loads(payload), 1):
			published = [(item["doctype"], item["row"])]
			published += [(child, row) for child, rows in item["children"].items() for row in rows]
			published += [("File", attachment["row"]) for attachment in item.get("files", [])]
			for doctype, raw in published:
				expected[doctype][str(raw["name"])] = stored_row(db, cache, doctype, raw)
				where[doctype, str(raw["name"])] = position
		for doctype, rows in expected.items():
			fields = list(next(iter(rows.values())))
			columns = ", ".join(f"`{field}`" for field in fields)
			# nosemgrep: frappe-semgrep-rules.rules.security.frappe-sql-format-injection -- reviewed: the table is a DocType of the publication profile and the columns were checked against the table's own (stored_row); the names are a parameter
			stored = frappe.db.sql(
				f"select {columns} from `tab{doctype}` where name in %(names)s",
				{"names": tuple(rows)},
				as_dict=True,
			)
			found = {str(row["name"]): row for row in stored}
			compared[doctype] += len(rows)
			for name, row in rows.items():
				if name not in found:
					missing[doctype] += 1
					continue
				for field, value in row.items():
					if not _equal(found[name][field], value):
						if doctype == "Activity" and field in ADDRESSES[0] and value in (None, ""):
							# left empty by the publisher and filled from the activity's Place (derive); the
							# integrity rules check it against the Place's address
							completed += 1
							continue
						key = f"{doctype}.{field}"
						differing[key] += 1
						places.setdefault(key, [sequence, where[doctype, name]])
	return {
		"rows_compared": dict(compared),
		"rows_missing": dict(missing),
		"place_fields_completed": completed,
		"fields_differing": dict(differing),
		"first_place": places,
	}


def stored_counts(context: str, manifest: dict) -> dict:
	"""Rows per table of the delegation, counted on the target."""
	out = {}
	for doctype in manifest["counts"]:
		source, values = _scope(doctype, context)
		# nosemgrep: frappe-semgrep-rules.rules.security.frappe-sql-format-injection -- reviewed: the scope is built in code from the publication profile (frozen.selection); the context is a parameter
		out[doctype] = frappe.db.sql(f"select count(*) {source}", values)[0][0]
	return out


def _receipt_chain(db, run: str, applied: int) -> bool:
	"""Whether the committed receipts are the publication's batches, in order."""
	published = db.execute(
		"select sequence, sha from batches where sequence <= ? order by sequence", (applied,)
	).fetchall()
	committed = frappe.db.sql(
		"""select sequence_number, digest from `tabUCM Migration Batch`
		where migration_run = %s order by sequence_number""",
		run,
	)
	return [tuple(row) for row in published] == [tuple(row) for row in committed]


def _sample(db, applied: int, every: int) -> int:
	"""One record in `every`, through the document route's own check of a stored record: the published fields, the
	option sets and the context rule as the controllers apply them, the children, the picture's bytes."""
	from ucm_migration_loader.publication_worker import reconcile_item

	cache, checked, seen = {}, 0, 0
	for sequence, payload in db.execute(
		"select sequence, payload from batches where sequence <= ? order by sequence", (applied,)
	):
		for position, item in enumerate(json.loads(payload), 1):
			seen += 1
			if seen % every:
				continue
			try:
				reconcile_item(db, cache, item)
			except Exception as exc:
				exc.migration_location = (sequence, position, item["doctype"])
				raise
			checked += 1
	return checked


def verify(run: str, sample_every: int = 50) -> dict:
	"""Check a derived load and, when nothing is found, make the run ready for the go-live decision. The report
	is kept on the run either way."""
	from ucm_migration_loader import publication_worker as documents
	from ucm_migration_loader.publication_shares import check_share

	api._operator()
	seconds, started = {}, monotonic()
	with _publication(run) as db:
		manifest = _checked_manifest(db, run)
		ctx, record = lock_run(run)
		_same_target(manifest, ctx, record)
		applied = record.applied or 0
		if record.state != APPLYING or applied != _business_batches(db):
			raise ValueError(_("Verify follows the last business batch of an applying run"))
		check_closed(ctx, pilot=False)
		documents.verify_sensitive_policy(manifest)
		context = ctx.name
		deferred = {doctype: manifest["counts"][doctype] for doctype in DEFERRED}
		expected = {dt: (0 if dt in DEFERRED else count) for dt, count in manifest["counts"].items()}

		def timed(step, call):
			at = monotonic()
			result = call()
			seconds[step] = round(monotonic() - at, 2)
			return result

		counted = timed("count", lambda: stored_counts(context, manifest))
		rows = timed("compare with the publication", lambda: compare(db, context, applied))
		rules = timed("integrity rules", lambda: integrity(context, [dt for dt, n in counted.items() if n]))
		sampled = timed("sampled document checks", lambda: _sample(db, applied, sample_every))
		access = Counter()
		for (payload,) in db.execute(
			"select payload from batches where sequence > ? order by sequence", (applied,)
		):
			items = json.loads(payload)
			if items[0]["doctype"] == "Record Share":
				for item in items:
					access[check_share(item["row"], context)] += 1
		findings = {
			"row counts": {dt: [counted[dt], expected[dt]] for dt in expected if counted[dt] != expected[dt]},
			"rows missing": rows["rows_missing"],
			"fields differing": rows["fields_differing"],
			"rules broken": rules["broken"],
			"receipts": [] if _receipt_chain(db, run, applied) else ["differ from the publication"],
		}
		findings = {kind: found for kind, found in findings.items() if found}
		report = {
			"route": ROUTE,
			"context": context,
			"published_rows": manifest["counts"],
			"stored_rows": counted,
			"rows_compared": rows["rows_compared"],
			"place_fields_completed": rows["place_fields_completed"],
			"rules_checked": rules["rules_checked"],
			"sampled_document_checks": sampled,
			"deferred_access_rows": deferred,
			"batch_chain_sha256": manifest["batch_chain_sha256"],
			"committed_business_batches": applied,
			"deferred_batches": record.received - applied,
			"share_review": dict(access),
			"findings": findings,
			"first_place": {key: rows["first_place"][key] for key in rows["fields_differing"]},
			"seconds": seconds,
		}
		frappe.db.set_value(
			api.RUN,
			run,
			{
				"state": APPLYING if findings else READY,
				"reconciliation_json": json.dumps(report),
				"finding": _("Verification found: {0}").format(", ".join(findings)) if findings else None,
			},
		)
		# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: the verification report and the run's state, kept whether or not it passed
		frappe.db.commit()
	return {
		**report,
		"state": READY if not findings else APPLYING,
		"total_seconds": round(monotonic() - started, 2),
	}


# ---------------------------------------------------------------------------------------------------------------
# activate


def activate(run: str, commit: bool = True) -> dict:
	"""Open a verified delegation: the assignments and shares held back are released through the documents, then
	the context is opened, in one transaction. The rows were verified in committed steps while the delegation was
	hidden, and its context has been locked since; here only the counts and the receipts are read again.
	`commit`: off to time an activation and leave the delegation hidden."""
	from ucm_migration_loader import held_shares
	from ucm_migration_loader import publication_worker as documents
	from ucm_migration_loader.publication_shares import check_share

	api._operator()
	started = monotonic()
	with _publication(run) as db:
		try:
			manifest = _checked_manifest(db, run)
			held_shares.lock_activation()
			ctx, record = lock_run(run)
			_same_target(manifest, ctx, record)
			report = json.loads(record.reconciliation_json or "{}")
			if record.state != READY or report.get("route") != ROUTE or report.get("findings"):
				raise ValueError(_("Activation follows a verification that found nothing"))
			check_closed(ctx, pilot=False)
			documents.verify_sensitive_policy(manifest)
			applied = record.applied
			if stored_counts(ctx.name, manifest) != report["stored_rows"] or not _receipt_chain(
				db, run, applied
			):
				raise ValueError(_("Stored rows or receipts changed since verification"))
			cache, held, released = {}, 0, Counter(dict.fromkeys(DEFERRED, 0))
			frappe.flags.ucm_publishing_context = ctx.name
			for sequence, sha, payload in db.execute(
				"select sequence, sha, payload from batches where sequence > ? order by sequence", (applied,)
			):
				items = json.loads(payload)
				doctype = items[0]["doctype"]
				for position, item in enumerate(items, 1):
					try:
						if item["doctype"] != doctype or doctype not in DEFERRED:
							raise ValueError(_("Unexpected activation batch"))
						if (
							doctype == "Record Share"
							and check_share(item["row"], ctx.name) == "receiver_not_active"
						):
							held_shares.hold(record, item["row"], sequence, position)
							held += 1
							continue
						documents.release_item(db, cache, item, ctx.name)
						released[doctype] += 1
					except Exception as exc:
						exc.migration_location = (sequence, position, doctype)
						raise
				_receipts(record, [(sequence, sha, items)], doctype, DOCUMENT)
			ctx.context_status = "Active"
			ctx.save()
			accounted = released.copy()
			accounted["Record Share"] += held
			if dict(accounted) != report["deferred_access_rows"]:
				raise ValueError(_("Release receipts differ from the deferred access inventory"))
			report.update(
				inbound_share_outcomes=held_shares.release_incoming(ctx, run),
				shares_held_at_activation=held,
				released_access_rows=dict(released),
			)
			frappe.db.set_value(
				api.RUN, run, {"applied": manifest["batches"], "reconciliation_json": json.dumps(report)}
			)
			api.mark_activated(run)
			result = {**api._result(api._record(run)), "seconds": round(monotonic() - started, 2)}
			if commit:
				# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: the go-live transaction: the access rows, the open context and the run's record commit together
				frappe.db.commit()
			else:
				frappe.db.rollback()
			return result
		except Exception:
			frappe.db.rollback()
			raise
		finally:
			frappe.flags.ucm_publishing_context = None


# ---------------------------------------------------------------------------------------------------------------
# discard


def delete_delegation(context: str, keep_files: bool = False) -> dict:
	"""Remove every row of the publication profile that belongs to a hidden delegation, the tables that depend on
	others first, by primary key, DELETE_ROWS at a time with a commit after each. Pictures no remaining File row
	names are removed from the disk afterwards. Returns the rows removed per table."""
	from ucm.permissions import counts

	removed, urls = Counter(), set()
	tables = []
	for doctype in reversed(ORDER):
		tables += [*CHILDREN.get(doctype, ()), *(("File",) if doctype == "Person" else ()), doctype]
	for doctype in tables:
		source, values = _scope(doctype, context)
		while True:
			# nosemgrep: frappe-semgrep-rules.rules.security.frappe-sql-format-injection -- reviewed: the scope is built in code from the publication profile (_scope); the context and the limit are parameters
			names = frappe.db.sql_list(
				f"select r.name {source} limit %(limit)s", {**values, "limit": DELETE_ROWS}
			)
			if not names:
				break
			if doctype == "File":
				urls.update(
					frappe.db.sql_list("select file_url from `tabFile` where name in %s", (tuple(names),))
				)
			with counts.moving(doctype, "x.name in %(names)s", {"names": tuple(names)}):
				# nosemgrep: frappe-semgrep-rules.rules.security.frappe-sql-format-injection -- reviewed: the table is a DocType of the publication profile; the names are a parameter
				frappe.db.sql(f"delete from `tab{doctype}` where name in %s", (tuple(names),))
			removed[doctype] += len(names)
			# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: a delete in chunks by design: no long transaction beside live delegations, and an interrupted delete is run again
			frappe.db.commit()
	if not keep_files and urls:
		# one read of the File table for all of them: file_url has no index
		used = set(
			frappe.db.sql_list("select distinct file_url from `tabFile` where file_url in %s", (tuple(urls),))
		)
		root = Path(frappe.get_site_path("private", "files")).resolve()
		for url in sorted(urls - used):
			path = (root / (url or "").removeprefix("/private/files/")).resolve()
			if url and url.startswith("/private/files/") and path.parent == root:
				path.unlink(missing_ok=True)
	return dict(removed)


def discard(run: str) -> dict:
	"""Remove a hidden run's rows and keep its receipts. A replacement publication takes a new snapshot identity."""
	api._operator()
	started = monotonic()
	ctx, record = lock_run(run)
	if record.state not in HIDDEN:
		raise ValueError(_("Only a hidden full publication can be discarded"))
	check_closed(ctx, pilot=False)
	frappe.db.set_value(
		api.RUN, run, {"state": CLEANING, "dispatch_id": (record.dispatch_id or 0) + 1, "finding": None}
	)
	# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: the run is marked as cleaning before its rows go, so that no worker applies to it meanwhile
	frappe.db.commit()
	removed = delete_delegation(ctx.name)
	manifest = json.loads(record.manifest_json)
	left = {dt: n for dt, n in stored_counts(ctx.name, manifest).items() if n}
	if left:
		raise ValueError(_("Rows remain after the delete: {0}").format(", ".join(sorted(left))))
	ctx, record = lock_run(run)
	frappe.db.set_value(api.RUN, run, {"state": "Discarded", "applied": 0})
	remove_after_discard(record)
	# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: the end of a discard run from the bench or a worker
	frappe.db.commit()
	return {"run": run, "state": "Discarded", "removed": removed, "seconds": round(monotonic() - started, 2)}
