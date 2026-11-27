"""Synthetic records for the tests: runs in the state a test needs, a small sealed publication built by hand,
and accounts with and without the operator's role.

The tests run on a site that may hold loaded delegations. What they make is their own: a run is named after a
random value, a context carries a digit in its code (ucm.tests.utils.make_context), and everything is rolled back."""

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import frappe

from ucm_migration_loader import api, publication_index
from ucm_migration_loader.contract import digest, encoded, table_signatures
from ucm_migration_loader.frozen import (
	CHILDREN,
	FORMAT,
	ORDER,
	TABLES,
	application_versions,
	reference_digest,
	referenced_dictionary_digest,
)
from ucm_migration_loader.publication_receiver import local_publication
from ucm_migration_loader.publication_transport import folder

PUBLISHED = {"Person": 100, "Activity": 300, "ToDo": 4, "Record Share": 10}
# what a set-based verification keeps for a delegation that came across whole
REPORT = {
	"route": "Set-based SQL",
	"published_rows": PUBLISHED,
	"stored_rows": {"Person": 100, "Activity": 300, "ToDo": 0, "Record Share": 0},
	"rows_compared": {"Person": 100, "Activity": 300},
	"derived_rows_changed": {"place geography": 7},
	"derive_seconds": 5.0,
	"rules_checked": 40,
	"sampled_document_checks": 8,
	"deferred_access_rows": {"ToDo": 4, "Record Share": 10},
	"batch_chain_sha256": "b" * 64,
	"committed_business_batches": 2,
	"findings": {},
	"first_place": {},
	"seconds": {"count": 1.0, "integrity rules": 2.0, "compare with the publication": 3.0},
}
# the same verification where a person is missing and a rule is broken
FOUND = {
	**REPORT,
	"stored_rows": {**REPORT["stored_rows"], "Person": 99},
	"findings": {
		"row counts": {"Person": [99, 100]},
		"rules broken": {"Activity": {"activity_status: not an option": 5}},
	},
}


def new_run_name() -> str:
	"""A run's name as a publication gives it: 64 hexadecimal characters, here of a random value."""
	return hashlib.sha256(uuid.uuid4().bytes).hexdigest()


def make_run(state="Publication ready", report=None, route="Set-based SQL", context=None, **values):
	"""A sealed publication's run as the loader leaves it in `state`, without a file or a loaded row."""
	manifest = {
		"format": FORMAT,
		"site": frappe.local.site,
		"snapshot": "TEST-1",
		"context": context or str(uuid.uuid4()),
		"counts": PUBLISHED,
		"schema": {"Person": "x", "Activity": "x"},
		"batches": 4,
	}
	record = frappe.get_doc(
		{
			"doctype": api.RUN,
			"context_id": manifest["context"],
			"snapshot_id": manifest["snapshot"],
			"manifest_json": encoded(manifest).decode(),
			"write_plan_json": json.dumps({"Person": route, "Activity": route}) if route else None,
			"state": state,
			"received": manifest["batches"],
			"applied": 0,
			"document_count": sum(PUBLISHED.values()),
			"reconciliation_json": json.dumps(report) if report else None,
			**values,
		}
	)
	# only the loader's own methods write a run; a test stands in for them
	record.insert(set_name=new_run_name(), ignore_permissions=True)
	return record


def make_receipt(run: str, sequence: int, **values):
	"""The receipt of one applied batch of a run."""
	receipt = frappe.get_doc(
		{
			"doctype": api.BATCH,
			"migration_run": run,
			"sequence_number": sequence,
			"digest": "c" * 64,
			**values,
		}
	)
	receipt.insert(set_name=f"{run}-{sequence}", ignore_permissions=True)
	return receipt


def make_account(name: str, operator: bool = False) -> str:
	"""An enabled account: a publishing account without a role, or an operator (System Manager)."""
	email = f"{name}@loader-test.example"
	if not frappe.db.exists("User", email):
		# the users a loaded site has just received count against Frappe's limit on users created per hour
		with patch.dict(frappe.conf, {"throttle_user_limit": 100_000}):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": name,
					"enabled": 1,
					"send_welcome_email": 0,
					"roles": [{"role": "System Manager"}] if operator else [],
				}
			).insert(ignore_permissions=True)
	return email


def hidden_context(code: str) -> str:
	"""A delegation's context as it waits for its load: locked and hidden, on a site without Frappe's sharing."""
	from ucm.tests.utils import make_context

	context = make_context(code)
	frappe.db.set_value("Context", context, {"context_status": "Locked", "context_discoverability": "Hidden"})
	frappe.db.set_single_value("System Settings", "disable_document_sharing", 1)
	return context


def site_schema() -> dict:
	"""The signatures of the published tables as this site has them."""
	return table_signatures(lambda sql: frappe.db.sql(sql)[0][1], TABLES, sort_indexes=True)


def seal(directory, context: str, persons=None, schema=None, versions=None) -> tuple[Path, str]:
	"""A sealed publication of one batch of persons, built as `ucm-push freeze` builds one, for this site.
	`schema` and `versions` default to the site's own, so that only what a test changes differs. Returns the
	file and the run it is."""
	persons = persons or [str(uuid.uuid4())]
	children = {child: [] for child in CHILDREN["Person"]}
	payload = encoded(
		[{"doctype": "Person", "row": {"name": name}, "children": children} for name in persons]
	)
	sha = hashlib.sha256(payload).hexdigest()
	counts = dict.fromkeys((*ORDER, *(c for names in CHILDREN.values() for c in names), "File"), 0)
	counts["Person"] = len(persons)
	path = Path(directory) / "publication.sqlite"
	with closing(sqlite3.connect(path)) as db:
		db.executescript(
			"""create table blobs (sha text unique, bytes blob not null);
			create table batches (sequence integer primary key, sha text not null, payload blob not null);
			create table refs (dt text, id text, payload blob, primary key (dt, id)) without rowid;
			create table descriptor (manifest blob not null);"""
		)
		db.execute("insert into batches (sha, payload) values (?, ?)", (sha, payload))
		manifest = {
			"format": FORMAT,
			"schema": schema or site_schema(),
			"schema_algorithm": "sorted-index-definitions-v1",
			"place_addresses_sealed": True,
			"sensitive_field_policy": {"fields": [], "exemptions": []},
			"application_versions": application_versions(frappe.db._conn) if versions is None else versions,
			"referenced_dictionary_sha256": referenced_dictionary_digest(db),
			"reference_sha256": reference_digest(db),
			"site": frappe.local.site,
			"snapshot": "TEST-1",
			"context": context,
			"counts": counts,
			"batch_chain_sha256": hashlib.sha256(bytes.fromhex(sha)).hexdigest(),
			"batches": 1,
			"unique_files": 0,
		}
		db.execute("insert into descriptor values (?)", (encoded(manifest),))
		db.commit()
	return path, digest(manifest)


def remove_files(run: str) -> None:
	"""Remove what staging or a transfer left on disk for a run: the database rolls back, the files do not."""
	import shutil

	path = local_publication(run)
	path.unlink(missing_ok=True)
	publication_index.index_path(path).unlink(missing_ok=True)
	shutil.rmtree(folder(run), ignore_errors=True)
