"""Build a portable, immutable-by-digest batch plan for generator-shaped delegations."""

import hashlib
import json
import os
import sqlite3
import tempfile
from collections import Counter
from contextlib import closing, contextmanager
from datetime import date, time, timedelta
from decimal import Decimal
from itertools import islice
from pathlib import Path

import pymysql

from .activity_order import DEPENDENCIES, activity_batches
from .contract import (
	MAX_PUBLICATION_BATCH_BYTES,
	MAX_PUBLICATION_BATCH_DOCUMENTS,
	check_uuid,
	digest,
	encoded,
	table_signatures,
)
from .dictionary_identity import TERM_FIELDS
from .scope import MISSING_PLACE

FORMAT = "ucm-casework-publication-v1"
ORDER = (
	"Organization",
	"Person",
	"Case",
	"Case Member",
	"Activity",
	"Activity Case",
	"Person Relationship",
	"Person Consent",
	"Person Note",
	"Activity Note",
	"Activity Detail",
	"Activity Detail Note",
	"Sensitive Information",
	"Record Tag",
	"ToDo",
	"Record Share",
)
CHILDREN = {
	"Person": ("Person Other Name", "Person Address"),
	"Case": ("Context Case",),
	"Activity": ("Activity Objective",),
}
OWNERS = {
	"Case Member": ("Case", "case_tech_id"),
	"Activity Case": ("Case", "case_tech_id"),
	"Person Relationship": ("Person", "person_tech_id"),
	"Person Consent": ("Person", "person_tech_id"),
	"Person Note": ("Person", "person_tech_id"),
	"Activity Detail": ("Activity", "activity_tech_id"),
	"Activity Detail Note": ("Activity", "activity_tech_id"),
	"Activity Note": ("Activity", "activity_tech_id"),
}


TABLES = (
	*ORDER,
	*(child for children in CHILDREN.values() for child in children),
	"File",
)


def application_versions(source, ucm_version=None):
	versions = {}
	with source.cursor() as cur:
		cur.execute("""select 1 from information_schema.tables where table_schema=database()
            and table_name='tabInstalled Application'""")
		if cur.fetchone():
			cur.execute("""select app_name,app_version from `tabInstalled Application`
                where app_name in ('frappe','ucm') order by app_name""")
			versions = dict(cur.fetchall())
	if ucm_version:
		if "ucm" in versions and versions["ucm"] != ucm_version:
			raise ValueError("Supplied UCM version differs from publishing metadata")
		versions["ucm"] = ucm_version
	return versions


def sensitive_policy(source, context, *, for_update=False):
	# A target release locks the assignments and exemptions until the context opens.
	lock = " for update" if for_update else ""
	with source.cursor() as cur:
		cur.execute(
			"""select s.record_doctype,s.field_name,c.clearance_name,c.clearance_kind
            from `tabSensitive Field` s left join `tabField Clearance` c on c.name=s.field_clearance_tech_id
            order by s.record_doctype,s.field_name,c.clearance_name,c.clearance_kind"""
			+ lock
		)
		fields = [list(row) for row in cur.fetchall()]
		cur.execute(
			"""select c.clearance_name,c.clearance_kind from `tabContext Profile Exemption` e
            left join `tabField Clearance` c on c.name=e.field_clearance_tech_id
            where e.context_tech_id=%s order by c.clearance_name,c.clearance_kind"""
			+ lock,
			(context,),
		)
		exemptions = [list(row) for row in cur.fetchall()]
	if any(not all(row) for row in (*fields, *exemptions)):
		raise ValueError("Sensitive field configuration has an unresolved clearance")
	return {"fields": fields, "exemptions": exemptions}


def publishing_schema(source):
	with source.cursor() as cur:

		def ddl(sql):
			cur.execute(sql)
			return cur.fetchone()[1]

		return table_signatures(ddl, TABLES, sort_indexes=True)


def scalar(value):
	if isinstance(value, (date, time)):
		return value.isoformat()
	if isinstance(value, (Decimal, timedelta)):
		return str(value)
	raise ValueError(f"Unsupported publishing type {type(value).__name__}")


def selection(dt, *, organizations=True):
	if dt in OWNERS:
		parent, field = OWNERS[dt]
		return f"join `tab{parent}` p on p.name=r.`{field}` where p.context_tech_id=%s"
	if dt == "Sensitive Information":
		kinds = "'Person','Activity','Activity Detail'"
		if organizations:
			kinds += ",'Organization'"
		return f"where r.context_tech_id=%s and r.record_doctype in ({kinds})"
	if dt == "ToDo":
		return """where exists(select 1 from tabPerson p where r.reference_type='Person' and p.name=r.reference_name and p.context_tech_id=%s)
            or exists(select 1 from tabCase c where r.reference_type='Case' and c.name=r.reference_name and c.context_tech_id=%s)"""
	return "where r.context_tech_id=%s"


def groups(source, dt, context, size):
	if dt == "Activity":
		with source.cursor(pymysql.cursors.SSCursor) as cur:
			cur.execute(DEPENDENCIES, context)
			yield from activity_batches(cur, size)
		return
	predicate = selection(dt)
	# Spool keys in one ordered source scan. A keyset query per batch can
	# repeatedly sort the entire joined activity/history inventory. Finish
	# reading the unbuffered cursor before fetching rows on this same snapshot.
	with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as keys:
		with source.cursor(pymysql.cursors.SSCursor) as cur:
			cur.execute(
				f"select r.name from `tab{dt}` r {predicate} order by r.name",
				(context,) * predicate.count("%s"),
			)
			for (key,) in cur:
				keys.write(json.dumps(key) + "\n")
		keys.seek(0)
		while batch := list(islice(keys, size)):
			yield [json.loads(line) for line in batch]


def store_picture(db, root, row):
	if not root or row["is_private"] != 1 or not row["file_url"].startswith("/private/files/"):
		raise ValueError("Private file storage is required for this publication")
	path = (root / row["file_url"].removeprefix("/private/files/")).resolve(strict=True)
	if not path.is_relative_to(root) or not path.is_file():
		raise ValueError("Invalid published private file")
	size = row["file_size"]
	if size != path.stat().st_size or size <= 0:
		raise ValueError("Private file size differs")
	sha = hashlib.sha256()
	md5 = hashlib.md5(usedforsecurity=False)
	with path.open("rb") as src:
		# Temporary row lets us stream even a large attachment without holding it in memory.
		slot = db.execute("insert into blobs(bytes) values(zeroblob(?))", (size,)).lastrowid
		with db.blobopen("blobs", "bytes", slot) as dest:
			while data := src.read(1024 * 1024):
				dest.write(data)
				sha.update(data)
				md5.update(data)
			if dest.tell() != size or md5.hexdigest() != row["content_hash"]:
				raise ValueError("Private file content differs from the published File row")
	key = sha.hexdigest()
	if db.execute("select 1 from blobs where sha=?", (key,)).fetchone():
		db.execute("delete from blobs where rowid=?", (slot,))
	else:
		db.execute("update blobs set sha=? where rowid=?", (key, slot))
	return key


LOOKUPS = {
	"Concept Value": TERM_FIELDS,
	"Protection Department": (
		"department_code",
		"department_name",
		"department_description",
		"department_is_enabled",
	),
	"Field Clearance": ("clearance_name", "clearance_kind", "clearance_description"),
	"User": ("email", "full_name"),
}


PLACE_ADDRESS_FIELDS = (
	"name",
	"idx",
	"country_tech_id",
	"place_address_admin_level_1_tech_id",
	"place_address_admin_level_2_tech_id",
	"place_address_admin_level_3_tech_id",
)


def capture_place_address(cur, db, key):
	if not key or db.execute("select 1 from refs where dt='Place Address' and id=?", (key,)).fetchone():
		return
	cur.execute(
		f"""select {",".join(PLACE_ADDRESS_FIELDS)} from `tabPlace Address`
        where parent=%s and parenttype='Place' and parentfield='place_addresses' order by idx,name limit 1""",
		(key,),
	)
	row = cur.fetchone()
	if row and row["idx"] == 0:
		cur.execute(
			"select count(*) as total from `tabPlace Address` where parent=%s and parenttype='Place' and parentfield='place_addresses'",
			(key,),
		)
		if cur.fetchone()["total"] != 1:
			raise ValueError("A zero address position is valid only for a singleton place address")
	db.execute("insert into refs values('Place Address',?,?)", (key, encoded(row)))


def capture_links(cur, db, link_fields, dt, row):
	for field, master in (
		*link_fields.get(dt, ()),
		("owner", "User"),
		("modified_by", "User"),
	):
		key = row.get(field)
		if not key or db.execute("select 1 from refs where dt=? and id=?", (master, key)).fetchone():
			continue
		cols = LOOKUPS[master]
		cur.execute(f"select {','.join(cols)} from `tab{master}` where name=%s", key)
		ref = cur.fetchone()
		if not ref:
			raise ValueError(f"Missing source reference for {dt}.{field}")
		db.execute("insert into refs values(?,?,?)", (master, key, encoded(ref)))


def referenced_dictionary_digest(db):
	sha = hashlib.sha256()
	for (payload,) in db.execute("select payload from refs where dt='Concept Value' order by payload"):
		sha.update(encoded(json.loads(payload)) + b"\n")
	return sha.hexdigest()


def reference_digest(db):
	h = hashlib.sha256()
	for dt, key, payload in db.execute("select dt,id,payload from refs order by dt,id"):
		h.update(encoded([dt, key, json.loads(payload)]) + b"\n")
	return h.hexdigest()


def records(source, db, root, dt, keys, counts, link_fields):
	with source.cursor(pymysql.cursors.DictCursor) as cur:
		cur.execute(f"select * from `tab{dt}` where name in %s", (tuple(keys),))
		parents = {r["name"]: r for r in cur.fetchall()}
		for key in keys:
			item = {"doctype": dt, "row": parents[key], "children": {}}
			capture_links(cur, db, link_fields, dt, parents[key])
			if dt == "Activity":
				capture_place_address(cur, db, parents[key].get("activity_place_tech_id"))
			counts[dt] += 1
			for child in CHILDREN.get(dt, ()):
				cur.execute(f"select * from `tab{child}` where parent=%s order by idx,name", key)
				rows = list(cur.fetchall())
				item["children"][child] = rows
				for r in rows:
					capture_links(cur, db, link_fields, child, r)
				counts[child] += len(rows)
			if dt == "Person":
				cur.execute(
					"select * from tabFile where attached_to_doctype='Person' and attached_to_name=%s order by name",
					key,
				)
				files = list(cur.fetchall())
				for file in files:
					capture_links(cur, db, link_fields, "File", file)
				item["files"] = [{"row": row, "sha256": store_picture(db, root, row)} for row in files]
				counts["File"] += len(files)
			yield json.dumps(
				item,
				ensure_ascii=False,
				sort_keys=True,
				allow_nan=False,
				separators=(",", ":"),
				default=scalar,
			).encode()


def write_place_findings(cur, path, site, snapshot, context):
	cur.execute(
		"select a.name, a.activity_type_tech_id, t.activity_type_place_type_tech_id "
		+ MISSING_PLACE
		+ " order by a.name",
		context,
	)
	with tempfile.TemporaryDirectory(dir=path.parent) as work:
		report = Path(work) / "findings"
		report.touch(mode=0o600)
		with report.open("w", encoding="utf-8") as out:
			for activity, activity_type, place_type in cur:
				finding = {
					"site": site,
					"snapshot": snapshot,
					"context": context,
					"doctype": "Activity",
					"name": activity,
					"field": "activity_place_tech_id",
					"rule": "required_place",
					"activity_type": activity_type,
					"required_place_type": place_type,
				}
				out.write(json.dumps(finding, ensure_ascii=False) + "\n")
		os.link(report, path)


def freeze(
	source,
	output,
	root,
	site,
	snapshot,
	context,
	size,
	byte_limit,
	findings_out=None,
	*,
	ucm_version=None,
):
	check_uuid(context)
	if not 1 <= size <= MAX_PUBLICATION_BATCH_DOCUMENTS or not 1 <= byte_limit <= MAX_PUBLICATION_BATCH_BYTES:
		raise ValueError("Invalid publication batch limits")
	if output.exists():
		raise FileExistsError(output)
	if findings_out is not None and (findings_out.resolve() == output.resolve() or findings_out.exists()):
		raise ValueError("Choose an unused findings path distinct from the publication")
	root = root.resolve(strict=True) if root else None
	with source.cursor() as cur:
		cur.execute("select context_code from tabContext where name=%s", context)
		if not cur.fetchone():
			raise ValueError("Unknown published delegation")
		cur.execute("select count(*) " + MISSING_PLACE, context)
		if missing := cur.fetchone()[0]:
			if findings_out is not None:
				write_place_findings(cur, findings_out, site, snapshot, context)
			subject = "activity lacks" if missing == 1 else "activities lack"
			raise ValueError(f"{missing} {subject} a required place")
	from .source_inventory import files_refusal, inventory

	held = inventory(source, context)
	# before any row is read: the site would refuse such a person at its load, after the transfer
	if refusal := files_refusal(held.get("unsupported_person_files") or {}):
		raise ValueError(refusal)
	if gaps := held["unsupported_business_rows"]:
		raise ValueError(
			f"Delegation contains business rows outside the publication profile: {json.dumps(gaps, sort_keys=True)}"
		)
	counts = Counter(
		dict.fromkeys(
			(
				*ORDER,
				"Person Address",
				"Person Other Name",
				"Activity Objective",
				"Context Case",
				"File",
			),
			0,
		)
	)
	with tempfile.TemporaryDirectory(dir=output.parent) as work:
		tmp = Path(work) / "publication"
		tmp.touch(mode=0o600)
		with closing(sqlite3.connect(tmp)) as db:
			db.executescript("""create table blobs (sha text unique, bytes blob not null);
              create table batches (sequence integer primary key, sha text not null, payload blob not null);
              create table refs(dt text,id text,payload blob,primary key(dt,id)) without rowid;
              create table descriptor (manifest blob not null);""")
			chain = hashlib.sha256()

			def flush(batch):
				payload = b"[" + b",".join(batch) + b"]"
				sha = hashlib.sha256(payload).hexdigest()
				db.execute("insert into batches(sha,payload) values(?,?)", (sha, payload))
				chain.update(bytes.fromhex(sha))

			with source.cursor() as cur:
				cur.execute(
					"select parent,fieldname,options from tabDocField where parent in %s and fieldtype='Link' and options in %s",
					(tuple(counts), tuple(LOOKUPS)),
				)
				link_fields = {}
				for dt, field, master in cur.fetchall():
					link_fields.setdefault(dt, []).append((field, master))
			for dt in ORDER:
				for keys in groups(source, dt, context, size):
					batch = []
					length = 2
					for doc in records(source, db, root, dt, keys, counts, link_fields):
						if len(doc) + 2 > byte_limit:
							raise ValueError(f"One {dt} document exceeds the selected batch byte limit")
						if batch and length + len(doc) + 1 > byte_limit:
							flush(batch)
							batch = []
							length = 2
						length += len(doc) + bool(batch)
						batch.append(doc)
					if batch:
						flush(batch)
			manifest = {
				"format": FORMAT,
				"schema": publishing_schema(source),
				"schema_algorithm": "sorted-index-definitions-v1",
				"place_addresses_sealed": True,
				"sensitive_field_policy": sensitive_policy(source, context),
				"application_versions": application_versions(source, ucm_version),
				"referenced_dictionary_sha256": referenced_dictionary_digest(db),
				"reference_sha256": reference_digest(db),
				"site": site,
				"snapshot": snapshot,
				"context": context,
				"counts": dict(counts),
				"batch_chain_sha256": chain.hexdigest(),
				"batches": db.execute("select count(*) from batches").fetchone()[0],
				"unique_files": db.execute("select count(*) from blobs").fetchone()[0],
			}
			db.execute("insert into descriptor values(?)", (encoded(manifest),))
			db.commit()
		os.link(tmp, output)
	return {"run": digest(manifest), **manifest}


@contextmanager
def open_sealed(path, *, immutable=False, one_view=True):
	"""A sealed file, opened to be read and nothing else. `immutable`: the site's own staged copy, which nothing
	writes any more. `one_view`: every read sees the file as it was at the first."""
	uri = Path(path).resolve(strict=True).as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
	with closing(sqlite3.connect(uri, uri=True)) as db:
		db.execute("pragma trusted_schema=OFF")
		if one_view:
			db.execute("begin")
		yield db


@contextmanager
def source_snapshot(config):
	"""One unchanging view of the publishing database, for everything a sealing reads. `config`: the private file
	of the read-only connection."""
	with pymysql.connect(**json.loads(Path(config).expanduser().read_text())) as source:
		with source.cursor() as cursor:
			cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
			cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
		yield source


def file_identity(path) -> tuple[str, int]:
	"""A sealed file's fingerprint and size: what an approval is bound to."""
	with Path(path).open("rb") as file:
		return hashlib.file_digest(file, "sha256").hexdigest(), os.fstat(file.fileno()).st_size


def hand_off(path, run, manifest, delegation=None) -> dict:
	"""What the site approves in place of a sealed file: its run, its manifest, and the file's fingerprint and
	size. `delegation`: its code and name, from which the site makes the context where it has none."""
	fingerprint, size = file_identity(path)
	about = {"delegation": delegation} if delegation else {}
	return {"run": run, "verified": True, "file_sha256": fingerprint, "file_bytes": size, **about, **manifest}


def verify_publication(path, run):
	"""Read a consistent view of the sealed publication and compare it to a separately retained run ID."""
	with open_sealed(path) as db:
		return verify_connection(db, run)


def locate_item(path, run, site, sequence, position):
	"""Resolve a recorded failure location to a published key in one verified read."""
	with open_sealed(path) as db:
		manifest = verify_connection(db, run)
		if manifest["site"] != site:
			raise ValueError("Publication targets another site")
		batch = db.execute("select payload from batches where sequence=?", (sequence,)).fetchone()
		if not batch:
			raise ValueError("Unknown batch number")
		items = json.loads(batch[0])
		if not 1 <= position <= len(items):
			raise ValueError("Item position is outside the batch")
		item = items[position - 1]
		return {
			"run": run,
			"site": site,
			"context": manifest["context"],
			"snapshot": manifest["snapshot"],
			"sequence": sequence,
			"item": position,
			"doctype": item["doctype"],
			"name": item["row"]["name"],
		}


def verify_connection(db, run):
	descriptor = db.execute("select manifest from descriptor").fetchall()
	if len(descriptor) != 1:
		raise ValueError("Invalid descriptor count")
	manifest = json.loads(descriptor[0][0])
	if manifest.get("format") != FORMAT or digest(manifest) != run:
		raise ValueError("Publication does not match the selected run")
	if "referenced_dictionary_sha256" in manifest and manifest[
		"referenced_dictionary_sha256"
	] != referenced_dictionary_digest(db):
		raise ValueError("Published dictionary identity inventory differs")
	if manifest.get("reference_sha256") != reference_digest(db):
		raise ValueError("Published reference identity differs")
	chain = hashlib.sha256()
	counts = Counter(dict.fromkeys(manifest["counts"], 0))
	last = 0
	for sequence, sha, payload in db.execute("select sequence,sha,payload from batches order by sequence"):
		if sequence != last + 1 or hashlib.sha256(payload).hexdigest() != sha:
			raise ValueError("Publication batch sequence or contents differ")
		last = sequence
		chain.update(bytes.fromhex(sha))
		for doc in json.loads(payload):
			dt = doc["doctype"]
			counts[dt] += 1
			for child, rows in doc["children"].items():
				counts[child] += len(rows)
			for attachment in doc.get("files", []):
				counts["File"] += 1
				if not db.execute("select 1 from blobs where sha=?", (attachment["sha256"],)).fetchone():
					raise ValueError("Published attachment bytes are missing")
	if (
		last != manifest["batches"]
		or chain.hexdigest() != manifest["batch_chain_sha256"]
		or dict(counts) != manifest["counts"]
	):
		raise ValueError("Publication differs from the batch manifest")
	unique = 0
	for rowid, key in db.execute("select rowid,sha from blobs"):
		with db.blobopen("blobs", "bytes", rowid, readonly=True) as file:
			sha = hashlib.sha256()
			while chunk := file.read(1024 * 1024):
				sha.update(chunk)
			if sha.hexdigest() != key:
				raise ValueError("Published attachment bytes differ")
		unique += 1
	if unique != manifest["unique_files"]:
		raise ValueError("Publication blob inventory differs")
	return manifest
