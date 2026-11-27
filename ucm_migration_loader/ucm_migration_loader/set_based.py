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

An operator chooses the route for a staged run (write_routes). The migration queue then carries the run with
`job`: each job takes one step from where the last one left it, and the steps that read the publication stop
after BUDGET seconds, so that a pause is answered soon and no job outlives its worker. `apply`, `derive` and
`verify` are the same steps called from the bench, one at a time.

No controller runs for a copied row, so every rule a save applies has to be named here: `integrity` lists the
ones checked as queries, and the module's tests compare a load by this route with the same load through the
documents. Sensitivity tags, assignments and shares stay on the document route: their rules read other
delegations, consent and clearances, and they are about one row in a hundred.
"""

import hashlib
import json
import os
import tempfile
from collections import Counter, defaultdict
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from time import monotonic

import frappe
from frappe import _
from frappe.utils import get_datetime, getdate, now_datetime

from ucm_migration_loader import api, publication_index
from ucm_migration_loader.application import check_closed, lock_run
from ucm_migration_loader.frozen import CHILDREN, LOOKUPS, ORDER, selection
from ucm_migration_loader.publication_receiver import (
	indexed_publication,
	release_differences,
	remove_after_discard,
	schema_differences,
)

from . import run_overview

ROUTE = "Set-based SQL"
DOCUMENT = "Document API"
# Their rules read clearances, consent and other delegations: written through the documents on every route
THROUGH_DOCUMENTS = frozenset(("Record Tag",))
DEFERRED = ("ToDo", "Record Share")  # released at activation
STAGED, APPLYING, PAUSED, READY, CLEANING = (
	"Publication staged",
	"Publication applying",
	"Publication paused",
	"Publication ready",
	"Publication cleaning",
)
HIDDEN = (STAGED, APPLYING, PAUSED, READY, CLEANING)
INSERT_ROWS = 500  # rows per INSERT statement
COMMIT_ROWS = 5000  # parent rows per transaction: small enough for the replicas to follow
DELETE_ROWS = 5000
SAMPLE_EVERY = 50  # one record in so many goes through the document route's check
BUDGET = 120  # seconds of batches a queued job takes before the next job continues
CHECKPOINT = 10  # seconds between two saves of a verification's cursor
# a rebuild, the rule queries and the activation of a large delegation cannot be cut in two
JOB_TIMEOUT = 3600
NUMERIC = ("Int", "Float", "Currency", "Percent", "Check", "Rating", "Duration")
NO_COLUMN = ("Section Break", "Column Break", "Tab Break", "HTML", "Button", "Heading", "Fold")


class _Sealed:
	"""The staged file, read only, in one snapshot. Every batch read is checked against the hash index written
	when the whole file was verified at staging, so a job does not hash the file again."""

	def __init__(self, db, hashes, manifest: dict, path, stamp):
		self.db, self.hashes, self.manifest, self.path, self.stamp = db, hashes, manifest, path, stamp

	def batches(self, after: int, upto: int | None = None):
		"""(sequence, sha, payload) of the batches after one and up to another, in order."""
		total = self.manifest["batches"]
		for sequence in range(after + 1, (total if upto is None else min(upto, total)) + 1):
			sha, payload = publication_index.read_batch(self.db, self.hashes, sequence, total)
			yield sequence, sha, payload

	def unchanged(self) -> None:
		publication_index.check_identity(self.path, self.stamp)


@contextmanager
def _publication(run: str):
	"""The run's staged file, opened for one step, on a target that still is the one it was reviewed for."""
	_ctx, record = lock_run(run)
	with indexed_publication(run, record) as (db, hashes, manifest, path, stamp):
		if release_differences(manifest):
			raise ValueError(_("Target application versions differ from the reviewed publication"))
		if schema_differences(manifest):
			raise ValueError(_("Target schema differs from publication"))
		# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: frees the run for a pause before a long read; nothing is pending but the index stamp of a publication staged before the index existed
		frappe.db.commit()
		yield _Sealed(db, hashes, manifest, path, stamp)


def _same_target(manifest: dict, ctx, record) -> None:
	if (manifest["site"], manifest["context"], manifest) != (
		frappe.local.site,
		ctx.name,
		json.loads(record.manifest_json),
	):
		raise ValueError(_("Staged publication target differs"))


@contextmanager
def _counts_moving(doctype: str, names: tuple):
	"""Around rows written or removed by SQL: the record counts of UCM's home move with them, in the same
	transaction, on a UCM that keeps such counts (ucm.permissions.counts). A UCM without them has none to move."""
	try:
		from ucm.permissions import counts
	except ImportError:
		yield
		return
	with counts.moving(doctype, "x.name in %(names)s", {"names": names}):
		yield


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
	for sequence, _sha, items, _size in group:
		for position, item in enumerate(items, 1):
			try:
				_item_rows(db, cache, context, doctype, item, rows)
			except Exception as exc:
				exc.migration_location = (sequence, position, doctype)
				raise
	return rows


def _receipts(record, group: list, doctype: str, route: str, seconds: float = 0) -> None:
	"""A receipt per batch, as the document route leaves: written in one statement, the loader's own DocType.
	The time a group took is shared among its batches by their records."""
	from ucm_migration_loader.display_ids import receipt_id

	now, user = now_datetime(), frappe.session.user
	records = sum(len(items) for _sequence, _sha, items, _size in group) or 1
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
		"parent_rows",
		"payload_bytes",
		"write_seconds",
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
				len(items),
				size,
				seconds * len(items) / records,
			)
			for sequence, sha, items, size in group
		),
	)


def _groups(batches):
	"""Consecutive batches of one DocType, up to COMMIT_ROWS parents: what one transaction writes. `batches`
	are the business batches still to write, as (sequence, sha, payload)."""
	group, doctype, size = [], None, 0
	for sequence, sha, payload in batches:
		items = json.loads(payload)
		kind = items[0]["doctype"]
		if kind in DEFERRED:
			raise ValueError(_("Assignments and shares must close the publication"))
		if kind not in ORDER:
			raise ValueError(_("Unsupported business batch"))
		if group and (kind != doctype or size >= COMMIT_ROWS):
			yield doctype, group
			group, size = [], 0
		doctype = kind
		group.append((sequence, sha, items, len(payload)))
		size += len(items)
	if group:
		yield doctype, group


def _progress(record) -> dict:
	"""What the steps after the last write have done, kept on the run between jobs."""
	progress = json.loads(record.get("reconcile_counts") or "{}")
	return progress if progress.get("route") == ROUTE else {"route": ROUTE}


def _mark(run: str, phase: str, **values) -> None:
	"""Show on the run what its worker is doing. The phase is stored in English and translated on the form."""
	frappe.db.set_value(api.RUN, run, {"worker_phase": phase, "worker_seen": now_datetime(), **values})


def _mine(record, ticket: int | None) -> bool:
	"""Whether a job still holds the run: no pause, discard or newer request has taken it."""
	return record.state in (STAGED, APPLYING) and (ticket is None or record.dispatch_id == ticket)


def _write(
	run: str, sealed: _Sealed, upto: int, *, ticket: int | None = None, deadline: float | None = None
) -> dict:
	"""Write the business batches not yet applied, up to one of them, from the last committed one on. Each group
	of batches, its receipts and the run's cursor commit together, so an interrupted write resumes where it
	stopped."""
	from ucm_migration_loader import migration_load
	from ucm_migration_loader import publication_worker as documents
	from ucm_migration_loader.publication_prerequisites import verify_links

	written, seconds, cache = Counter(), Counter(), {}
	_ctx, record = lock_run(run)
	start = record.applied or 0
	frappe.db.rollback()
	for doctype, group in _groups(sealed.batches(start, upto)):
		try:
			ctx, record = lock_run(run)
			_same_target(sealed.manifest, ctx, record)
			if (
				not _mine(record, ticket)
				or (record.applied or 0) != group[0][0] - 1
				or (deadline is not None and monotonic() > deadline)
			):
				frappe.db.rollback()
				break  # paused, superseded, moved by another worker or out of time: the next job continues
			if reason := migration_load.pressure():
				_mark(run, "Paused for live load", state=PAUSED, finding=reason)
				# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: the pause and its reason, kept for the operator who resumes the run
				frappe.db.commit()
				break
			check_closed(ctx, pilot=False)
			if not record.applied:
				step = monotonic()
				documents.verify_sensitive_policy(sealed.manifest)
				verify_links(sealed.db)
				seconds["resolve every reference"] = monotonic() - step
			step = monotonic()
			if doctype in THROUGH_DOCUMENTS:
				for sequence, _sha, items, _size in group:
					documents.insert_batch(sealed.db, ctx.name, items, sequence)
				route = DOCUMENT
				written[doctype] += sum(len(items) for _sequence, _sha, items, _size in group)
			else:
				rows = _group_rows(sealed.db, cache, ctx.name, doctype, group)
				seconds["of which reading the file and preparing rows"] += monotonic() - step
				names = tuple(row["name"] for row in rows[doctype])
				with _counts_moving(doctype, names):
					for table, table_rows in rows.items():
						_insert(table, table_rows)
						written[table] += len(table_rows)
				route = ROUTE
			_receipts(record, group, doctype, route, monotonic() - step)
			sealed.unchanged()
			_mark(run, "Writing " + doctype, state=APPLYING, applied=group[-1][0], finding=None)
			# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: a load in small transactions by design: the rows, their receipts and the cursor commit together, and the replicas follow
			frappe.db.commit()
			seconds["write " + doctype] += monotonic() - step
		except Exception:
			frappe.db.rollback()
			raise
	return {
		"rows_written": dict(written),
		"seconds": {step: round(value, 2) for step, value in seconds.items()},
	}


def _take_route(run: str) -> None:
	"""A run loaded by this route says so in its plan: the form, the list and the queue read the route there. A
	staged run is given the route; a run already started on another route is refused."""
	from ucm_migration_loader import write_routes

	record = api._record(run)
	if write_routes.set_based(json.loads(record.write_plan_json or "{}")):
		return
	if not write_routes.editable(record):
		raise ValueError(_("This run was started on another write route"))
	write_routes.select(run, ROUTE)


def apply(run: str, stop_at: int | None = None) -> dict:
	"""Write the business batches not yet applied, all of them or up to `stop_at`, in one call from the bench."""
	api._operator()
	_take_route(run)
	started = monotonic()
	with _publication(run) as sealed:
		opened = monotonic() - started
		last = _business_batches(sealed.db)
		out = _write(run, sealed, last if stop_at is None else min(last, stop_at))
	return {
		**api._result(api._record(run)),
		"rows_written": out["rows_written"],
		"seconds": {"open the sealed file": round(opened, 2), **out["seconds"]},
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


def _derive(context: str) -> dict:
	"""The values a save derives, for one delegation's rows, with UCM's own set-based rebuilds. Returns the rows
	each one changed: none, where the publication already carried what UCM computes. A rebuild is called only
	where rows differ, and writes only those, so the step can be run again."""
	from ucm.ucm.doctype.activity.activity import COUNTRY, sync_all_countries
	from ucm.ucm.doctype.activity_detail.latest_state import BATCH, refresh_persons
	from ucm.ucm.doctype.protection_department.protection_department import (
		ACTIVITY_CARRIERS,
		FIELD,
		sync_all_details,
	)
	from ucm.ucm.doctype.record_share.record_share import rebuild_activity_facts

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
	return changed


def _derive_step(run: str, *, ticket: int | None = None) -> dict | None:
	"""Derive the delegation's values and note on the run that it is done. Nothing, where the run was taken."""
	ctx, record = lock_run(run)
	if not _mine(record, ticket):
		frappe.db.rollback()
		return None
	check_closed(ctx, pilot=False)
	_mark(run, "Deriving", state=APPLYING, finding=None)
	# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: shows the step on the form and frees the run for a pause while the rebuilds work
	frappe.db.commit()
	started = monotonic()
	changed = _derive(ctx.name)
	seconds = round(monotonic() - started, 2)
	_ctx, record = lock_run(run)
	if not _mine(record, ticket):
		frappe.db.rollback()
		return None
	progress = {"route": ROUTE, "derived": changed, "derive_seconds": seconds}
	_mark(run, "Deriving", reconcile_cursor=0, reconcile_counts=json.dumps(progress))
	# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: the end of a step, so that the next job starts at the verification
	frappe.db.commit()
	return {"changed": changed, "seconds": seconds}


def derive(run: str) -> dict:
	"""Derive the values of an applying run's delegation, in one call from the bench."""
	api._operator()
	_ctx, record = lock_run(run)
	if record.state != APPLYING:
		raise ValueError(_("Derive follows the last business batch of an applying run"))
	return _derive_step(run)


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
			values[f"offered{number}"] = tuple(str(value) for value in offered) or ("",)
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
	if doctype == "Person Relationship":
		# PersonRelationship.validate: a person's relationship to another person is one row, whatever the
		# relationship; the relationship the other way round is another row
		rules["related person: this person's relationship to them is recorded twice"] = (
			"exists (select 1 from `tabPerson Relationship` m where m.person_tech_id = r.person_tech_id"
			" and m.person_related_tech_id = r.person_related_tech_id and m.name <> r.name)"
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


def _tally() -> dict:
	"""What a verification has found so far: kept on the run as JSON between jobs."""
	return {
		"rows_compared": {},
		"rows_missing": {},
		"fields_differing": {},
		"first_place": {},
		"place_fields_completed": 0,
		"records_seen": 0,
		"records_sampled": 0,
		"seconds": {"compare": 0.0, "sample": 0.0},
	}


def _add(counts: dict, key: str, number: int = 1) -> None:
	counts[key] = counts.get(key, 0) + number


def _compare_batch(db, cache: dict, sequence: int, items: list, tally: dict) -> None:
	"""Every stored column of a batch's rows against the publication. The tally takes the rows compared per
	table, and for the fields that differ how many rows and the first place (batch and item, never values)."""
	from ucm.ucm.doctype.activity.activity import ADDRESSES

	expected, where = defaultdict(dict), {}
	for position, item in enumerate(items, 1):
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
		_add(tally["rows_compared"], doctype, len(rows))
		for name, row in rows.items():
			if name not in found:
				_add(tally["rows_missing"], doctype)
				continue
			for field, value in row.items():
				if _equal(found[name][field], value):
					continue
				if doctype == "Activity" and field in ADDRESSES[0] and value in (None, ""):
					# left empty by the publisher and filled from the activity's Place (derive); the
					# integrity rules check it against the Place's address
					tally["place_fields_completed"] += 1
					continue
				key = f"{doctype}.{field}"
				_add(tally["fields_differing"], key)
				tally["first_place"].setdefault(key, [sequence, where[doctype, name]])


def _sample_batch(db, cache: dict, sequence: int, items: list, tally: dict, every: int) -> None:
	"""One record in `every`, through the document route's own check of a stored record: the published fields, the
	option sets and the context rule as the controllers apply them, the children, the picture's bytes."""
	from ucm_migration_loader.publication_worker import reconcile_item

	for position, item in enumerate(items, 1):
		tally["records_seen"] += 1
		if tally["records_seen"] % every:
			continue
		try:
			reconcile_item(db, cache, item)
		except Exception as exc:
			exc.migration_location = (sequence, position, item["doctype"])
			raise
		tally["records_sampled"] += 1


def stored_counts(context: str, manifest: dict) -> dict:
	"""Rows per table of the delegation, counted on the target."""
	out = {}
	for doctype in manifest["counts"]:
		source, values = _scope(doctype, context)
		# nosemgrep: frappe-semgrep-rules.rules.security.frappe-sql-format-injection -- reviewed: the scope is built in code from the publication profile (frozen.selection); the context is a parameter
		out[doctype] = frappe.db.sql(f"select count(*) {source}", values)[0][0]
	return out


def _receipt_chain(sealed: _Sealed, run: str, applied: int) -> bool:
	"""Whether the committed receipts are the publication's batches, in order."""
	published = sealed.db.execute(
		"select sequence, sha from batches where sequence <= ? order by sequence", (applied,)
	).fetchall()
	committed = frappe.db.sql(
		"""select sequence_number, digest from `tabUCM Migration Batch`
		where migration_run = %s order by sequence_number""",
		run,
	)
	return [tuple(row) for row in published] == [tuple(row) for row in committed]


def _keep(run: str, sealed: _Sealed, ticket: int | None, cursor: int, progress: dict) -> bool:
	"""Keep a verification's cursor and tally on the run. Whether the job still holds the run."""
	_ctx, record = lock_run(run)
	if not _mine(record, ticket):
		frappe.db.rollback()
		return False
	sealed.unchanged()
	_mark(run, "Verifying", state=APPLYING, reconcile_cursor=cursor, reconcile_counts=json.dumps(progress))
	# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: the verification's cursor and tally, so that a pause is answered and the next job continues after this batch
	frappe.db.commit()
	return True


def _check(
	run: str,
	sealed: _Sealed,
	last: int,
	*,
	ticket: int | None = None,
	deadline: float | None = None,
	sample_every: int = SAMPLE_EVERY,
) -> None:
	"""Compare the batches not yet compared with the stored rows, and sample their records. The tally and the
	cursor are kept on the run every CHECKPOINT seconds, so a pause is answered soon and the next job continues
	after the last batch kept. The run is not locked while its rows are read: its context is locked and hidden,
	and the last step counts the rows again."""
	_ctx, record = lock_run(run)
	frappe.db.rollback()
	if not _mine(record, ticket):
		return
	progress, cursor = _progress(record), record.reconcile_cursor or 0
	tally = progress["compared"] = (progress.get("compared") if cursor else None) or _tally()
	compared, sampled, kept = {}, {}, monotonic()
	for sequence, _sha, payload in sealed.batches(cursor, last):
		items = json.loads(payload)
		started = monotonic()
		_compare_batch(sealed.db, compared, sequence, items, tally)
		between = monotonic()
		_sample_batch(sealed.db, sampled, sequence, items, tally, sample_every)
		now = monotonic()
		tally["seconds"]["compare"] += between - started
		tally["seconds"]["sample"] += now - between
		out_of_time = deadline is not None and now > deadline
		if out_of_time or sequence == last or now - kept >= CHECKPOINT:
			if not _keep(run, sealed, ticket, sequence, progress) or out_of_time:
				return
			kept = monotonic()


def _conclude(run: str, sealed: _Sealed, last: int, *, ticket: int | None = None) -> dict | None:
	"""Count the delegation, check the integrity rules and the receipts, and keep the report on the run: ready
	for the go-live decision when nothing was found, paused with the findings otherwise. Nothing, where the run
	was taken."""
	from ucm_migration_loader import publication_worker as documents
	from ucm_migration_loader.publication_shares import check_share

	ctx, record = lock_run(run)
	frappe.db.rollback()
	if not _mine(record, ticket):
		return None
	manifest, context, seconds = sealed.manifest, ctx.name, {}
	_same_target(manifest, ctx, record)

	def timed(step, call):
		at = monotonic()
		result = call()
		seconds[step] = round(monotonic() - at, 2)
		return result

	counted = timed("count", lambda: stored_counts(context, manifest))
	rules = timed("integrity rules", lambda: integrity(context, [dt for dt, n in counted.items() if n]))
	access = Counter()
	for sequence, _sha, payload in sealed.batches(last):
		items = json.loads(payload)
		if items[0]["doctype"] == "Record Share":
			for position, item in enumerate(items, 1):
				try:
					access[check_share(item["row"], context)] += 1
				except Exception as exc:
					# a share UCM's rules no longer allow stops the load: its place goes to the finding
					exc.migration_location = (sequence, position, "Record Share")
					raise
	ctx, record = lock_run(run)
	if not _mine(record, ticket) or (record.applied or 0) != last or (record.reconcile_cursor or 0) != last:
		frappe.db.rollback()
		return None
	check_closed(ctx, pilot=False)
	documents.verify_sensitive_policy(manifest)
	progress = _progress(record)
	tally = progress.get("compared") or _tally()
	seconds["compare with the publication"] = round(tally["seconds"]["compare"], 2)
	seconds["sampled document checks"] = round(tally["seconds"]["sample"], 2)
	deferred = {doctype: manifest["counts"][doctype] for doctype in DEFERRED}
	expected = {dt: (0 if dt in DEFERRED else count) for dt, count in manifest["counts"].items()}
	findings = {
		"row counts": {dt: [counted[dt], expected[dt]] for dt in expected if counted[dt] != expected[dt]},
		"rows missing": tally["rows_missing"],
		"fields differing": tally["fields_differing"],
		"rules broken": rules["broken"],
		"receipts": [] if _receipt_chain(sealed, run, last) else ["differ from the publication"],
	}
	findings = {kind: found for kind, found in findings.items() if found}
	report = {
		"route": ROUTE,
		"context": context,
		"published_rows": manifest["counts"],
		"stored_rows": counted,
		"rows_compared": tally["rows_compared"],
		"derived_rows_changed": progress.get("derived"),
		"derive_seconds": progress.get("derive_seconds"),
		"place_fields_completed": tally["place_fields_completed"],
		"rules_checked": rules["rules_checked"],
		"sampled_document_checks": tally["records_sampled"],
		"deferred_access_rows": deferred,
		"batch_chain_sha256": manifest["batch_chain_sha256"],
		"committed_business_batches": last,
		"deferred_batches": record.received - last,
		"share_review": dict(access),
		"findings": findings,
		"first_place": {key: tally["first_place"][key] for key in tally["fields_differing"]},
		"seconds": seconds,
	}
	sealed.unchanged()
	if findings:
		# the next request derives and verifies again, from the first batch
		_mark(
			run,
			"Verification found differences",
			state=PAUSED,
			reconciliation_json=json.dumps(report),
			finding=_("Verification found: {0}").format(", ".join(findings)),
			reconcile_cursor=0,
			reconcile_counts=json.dumps({"route": ROUTE}),
		)
	else:
		_mark(run, "Ready for activation", state=READY, reconciliation_json=json.dumps(report), finding=None)
	run_overview.refresh(run)
	# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: the verification report and the run's state, kept whether or not it passed
	frappe.db.commit()
	return {**report, "state": PAUSED if findings else READY}


def verify(run: str, sample_every: int = SAMPLE_EVERY) -> dict:
	"""Check a derived load from its first batch on, in one call from the bench, and when nothing is found make
	the run ready for the go-live decision. The report is kept on the run either way."""
	api._operator()
	started = monotonic()
	with _publication(run) as sealed:
		last = _business_batches(sealed.db)
		_ctx, record = lock_run(run)
		if record.state != APPLYING or (record.applied or 0) != last:
			raise ValueError(_("Verify follows the last business batch of an applying run"))
		frappe.db.set_value(api.RUN, run, "reconcile_cursor", 0)
		# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: the verification starts again from the first batch
		frappe.db.commit()
		_check(run, sealed, last, sample_every=sample_every)
		report = _conclude(run, sealed, last)
	if report is None:
		raise ValueError(_("The run was paused or taken by another request"))
	return {**report, "total_seconds": round(monotonic() - started, 2)}


# ---------------------------------------------------------------------------------------------------------------
# the queue


def _next_step(run: str, last: int, stop_at: int | None, ticket: int | None) -> str | None:
	"""The step a load is at, read from the run: nothing where it is paused, taken, ready or at its limit."""
	_ctx, record = lock_run(run)
	frappe.db.rollback()
	if not _mine(record, ticket):
		return None
	applied = record.applied or 0
	if applied < last:
		return "write" if stop_at is None or applied < stop_at else None
	if "derived" not in _progress(record):
		return "derive"
	return "verify" if (record.reconcile_cursor or 0) < last else "conclude"


def advance(
	run: str,
	*,
	ticket: int | None = None,
	stop_at: int | None = None,
	budget: float | None = None,
	sample_every: int = SAMPLE_EVERY,
) -> dict:
	"""Take a load on from where the last call left it: the batches not yet written, then the derived values,
	then the verification. A queued job passes its ticket and a `budget` in seconds, takes one step and is
	followed by another job while `more` is returned; without a budget the call runs to the end."""
	api._operator()
	deadline, steps, more = None if budget is None else monotonic() + budget, {}, False
	with _publication(run) as sealed:
		last = _business_batches(sealed.db)
		while step := _next_step(run, last, stop_at, ticket):
			if step == "write":
				upto = last if stop_at is None else min(last, stop_at)
				steps[step] = _write(run, sealed, upto, ticket=ticket, deadline=deadline)
			elif step == "derive":
				steps[step] = _derive_step(run, ticket=ticket)
			elif step == "verify":
				_check(run, sealed, last, ticket=ticket, deadline=deadline, sample_every=sample_every)
			else:
				steps[step] = _conclude(run, sealed, last, ticket=ticket)
			if budget is not None:
				more = _next_step(run, last, stop_at, ticket) is not None
				break
	return {**api._result(api._record(run)), "steps": steps, "more": more}


def job(run: str, action: str, ticket: int, stop_at: int | None = None) -> bool:
	"""One queued job of a run on this route. Whether another job should follow it."""
	if action == "apply":
		every = max(1, int(frappe.conf.get("ucm_migration_sample_every") or SAMPLE_EVERY))
		return advance(run, ticket=ticket, stop_at=stop_at, budget=BUDGET, sample_every=every)["more"]
	if action == "activate":
		activate(run)
		return False
	if action == "discard":
		return discard(run, ticket=ticket, budget=BUDGET)["state"] == CLEANING
	raise ValueError(_("Unknown queued migration action"))


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
	with documents.current_reads(), _publication(run) as sealed:
		try:
			manifest = sealed.manifest
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
				sealed, run, applied
			):
				raise ValueError(_("Stored rows or receipts changed since verification"))
			cache, held, released = {}, 0, Counter(dict.fromkeys(DEFERRED, 0))
			frappe.flags.ucm_publishing_context = ctx.name
			for sequence, sha, payload in sealed.batches(applied):
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
						documents.release_item(sealed.db, cache, item, ctx.name)
						released[doctype] += 1
					except Exception as exc:
						exc.migration_location = (sequence, position, doctype)
						raise
				_receipts(record, [(sequence, sha, items, len(payload))], doctype, DOCUMENT)
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
			sealed.unchanged()
			_mark(run, "Activated", applied=manifest["batches"], reconciliation_json=json.dumps(report))
			api.mark_activated(run)
			result = {
				**api._result(api._record(run)),
				"released_access_rows": report["released_access_rows"],
				"shares_held_at_activation": held,
				"inbound_share_outcomes": report["inbound_share_outcomes"],
				"seconds": round(monotonic() - started, 2),
			}
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


def _delete(context: str, keep_files: bool = False, deadline: float | None = None) -> tuple[dict, bool]:
	"""Remove every row of the publication profile that belongs to a hidden delegation, the tables that depend on
	others first, by primary key, DELETE_ROWS at a time with a commit after each. Pictures no remaining File row
	names are removed from the disk afterwards. Returns the rows removed per table, and whether none is left:
	past the deadline the rest waits for the next call."""
	removed, urls, finished = Counter(), set(), True
	tables = []
	for doctype in reversed(ORDER):
		tables += [*CHILDREN.get(doctype, ()), *(("File",) if doctype == "Person" else ()), doctype]
	for doctype in tables:
		source, values = _scope(doctype, context)
		while finished:
			if deadline is not None and monotonic() > deadline:
				finished = False
				break
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
			with _counts_moving(doctype, tuple(names)):
				# nosemgrep: frappe-semgrep-rules.rules.security.frappe-sql-format-injection -- reviewed: the table is a DocType of the publication profile; the names are a parameter
				frappe.db.sql(f"delete from `tab{doctype}` where name in %s", (tuple(names),))
			removed[doctype] += len(names)
			# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: a delete in chunks by design: no long transaction beside live delegations, and an interrupted delete is run again
			frappe.db.commit()
	if not keep_files and urls:
		# one read of the File table for all of them: file_url has no index. A picture other rows of the
		# delegation still name stays until the call that removes the last of them
		used = set(
			frappe.db.sql_list("select distinct file_url from `tabFile` where file_url in %s", (tuple(urls),))
		)
		root = Path(frappe.get_site_path("private", "files")).resolve()
		for url in sorted(urls - used):
			path = (root / (url or "").removeprefix("/private/files/")).resolve()
			if url and url.startswith("/private/files/") and path.parent == root:
				path.unlink(missing_ok=True)
	return dict(removed), finished


def delete_delegation(context: str, keep_files: bool = False) -> dict:
	"""Remove a hidden delegation's rows, all of them. Returns the rows removed per table."""
	return _delete(context, keep_files)[0]


def discard(run: str, *, ticket: int | None = None, budget: float | None = None) -> dict:
	"""Remove a hidden run's rows and keep its receipts. A replacement publication takes a new snapshot identity.
	A queued job passes its ticket and a `budget` in seconds, and is followed by another job while the run stays
	cleaning; without them the call marks the run itself and removes everything."""
	api._operator()
	started = monotonic()
	ctx, record = lock_run(run)
	if ticket is None:
		if record.state not in HIDDEN:
			raise ValueError(_("Only a hidden full publication can be discarded"))
		frappe.db.set_value(
			api.RUN, run, {"state": CLEANING, "dispatch_id": (record.dispatch_id or 0) + 1, "finding": None}
		)
	elif record.state != CLEANING or record.dispatch_id != ticket:
		frappe.db.rollback()
		return {"run": run, "state": record.state, "removed": {}}
	check_closed(ctx, pilot=False)
	_mark(run, "Removing rows")
	# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: the run is marked as cleaning before its rows go, so that no worker applies to it meanwhile
	frappe.db.commit()
	removed, finished = _delete(ctx.name, deadline=None if budget is None else started + budget)
	out = {"run": run, "removed": removed, "seconds": round(monotonic() - started, 2)}
	if not finished:
		return {**out, "state": CLEANING}
	manifest = json.loads(record.manifest_json)
	left = {dt: n for dt, n in stored_counts(ctx.name, manifest).items() if n}
	if left:
		raise ValueError(_("Rows remain after the delete: {0}").format(", ".join(sorted(left))))
	_ctx, record = lock_run(run)
	if record.state != CLEANING or (ticket is not None and record.dispatch_id != ticket):
		frappe.db.rollback()
		return {**out, "state": record.state}
	_mark(run, "Discarded", state="Discarded", applied=0, reconcile_cursor=0)
	remove_after_discard(record)
	run_overview.refresh(run)
	# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: the end of a discard run from the bench or a worker
	frappe.db.commit()
	return {**out, "state": "Discarded", "seconds": round(monotonic() - started, 2)}
