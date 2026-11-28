"""Receive a sealed publication on the bench and bind it to a locked target context."""

import json
import os
import re
import shutil
import tempfile
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path
from time import monotonic

import frappe
from frappe import _

from . import constants, publication_index, runs
from .contract import encoded, table_signatures
from .frozen import (
	CHILDREN,
	ORDER,
	TABLES,
	application_versions,
	open_sealed,
	selection,
	verify_connection,
	verify_publication,
)


def local_publication(run):
	if not re.fullmatch(r"[a-f0-9]{64}", run):
		raise ValueError("Invalid publication run ID")
	return Path(frappe.get_site_path("private", "migration-publications", run + ".sqlite"))


def staged_file(run):
	"""The run's staged file, for a step that reads it. It leaves the site when the run is activated or
	discarded."""
	path = local_publication(run)
	if not path.is_file():
		raise ValueError("The run's file is no longer on this site")
	return path.resolve(strict=True)


def inbox(run):
	"""Where the pieces of a run's sealed file arrive, with the approval they are checked against."""
	local_publication(run)  # the identifier is checked before any path is made from it
	return Path(frappe.get_site_path("private", "migration-inbox", run))


def schema_differences(manifest):
	if "schema" not in manifest:
		return None  # Earlier sealed files predate the schema receipt.
	algorithm = manifest.get("schema_algorithm")
	if algorithm not in (None, "sorted-index-definitions-v1"):
		raise ValueError("Unsupported publication schema fingerprint")
	current = table_signatures(
		lambda sql: frappe.db.sql(sql)[0][1],
		[dt for dt in TABLES if dt != "Organization" or "Organization" in manifest["counts"]],
		sort_indexes=algorithm is not None,
	)
	return sorted(
		dt for dt in set(current) | set(manifest["schema"]) if current.get(dt) != manifest["schema"].get(dt)
	)


def release_differences(manifest):
	published = manifest.get("application_versions", {})
	if not published:
		return {}
	if not frappe.db._conn:
		frappe.db.connect()
	target = application_versions(frappe.db._conn)
	return {
		app: {"published": version, "target": target.get(app)}
		for app, version in published.items()
		if target.get(app) != version
	}


def preflight(path: str, run: str):
	"""Read-only inventory. Report counts without exposing record IDs or field values."""
	runs.only_admin()
	with open_sealed(path) as db:
		manifest = verify_connection(db, run)
		return _inventory(db, run, manifest)


def _inventory(db, run, manifest):
	if manifest["site"] != frappe.local.site:
		raise ValueError("Publication targets another site")
	collisions = Counter()
	held_collisions = 0
	for (payload,) in db.execute("select payload from batches order by sequence"):
		groups = defaultdict(list)
		for item in json.loads(payload):
			dt = item["doctype"]
			if dt not in ORDER or set(item["children"]) != set(CHILDREN.get(dt, ())):
				raise ValueError("Unexpected document type in publication")
			groups[dt].append(item["row"]["name"])
			for child, rows in item["children"].items():
				groups[child].extend(r["name"] for r in rows)
			groups["File"].extend(f["row"]["name"] for f in item.get("files", []))
		for dt, keys in groups.items():
			if keys:
				found = frappe.db.count(dt, {"name": ["in", keys]})
				if dt == "Record Share":
					from .held_shares import HELD

					held_collisions += frappe.db.count(HELD, {"name": ["in", keys]})
				if found:
					collisions[dt] += found
	return {
		"run": run,
		"schema_differences": schema_differences(manifest),
		"release_differences": release_differences(manifest),
		"includes_organizations": "Organization" in manifest["counts"],
		"existing_keys": dict(collisions),
		"existing_share_receipts": held_collisions,
		"context_exists": bool(frappe.db.exists("Context", manifest["context"])),
	}


REFUSALS = (
	("schema_differences", "tables differ from this site"),
	("release_differences", "application versions differ"),
	("existing_keys", "record IDs are already on this site"),
	("existing_share_receipts", "share receipts are already kept"),
)


def check_unchanged_site(manifest) -> None:
	"""The site still is the one the publication was reviewed for: the same application versions and the same
	tables. Asked again before every step that writes."""
	if release_differences(manifest):
		raise ValueError(_("Target application versions differ from the reviewed publication"))
	if schema_differences(manifest):
		raise ValueError(_("Target schema differs from publication"))


def same_target(manifest, context, record) -> bool:
	"""Whether a staged file is the one this run was staged with, for this site and this delegation."""
	return (manifest["site"], manifest["context"], manifest) == (
		frappe.local.site,
		context,
		json.loads(record.manifest_json),
	)


def check_same_target(manifest, context, record) -> None:
	if not same_target(manifest, context, record):
		raise ValueError(_("Staged publication target differs"))


def context_state(context: str, run: str | None = None) -> dict:
	"""Whether a delegation's context can receive a publication: it exists, is locked and hidden, holds no record
	of the publication's tables, and no other run holds it."""
	found = frappe.db.get_value(
		"Context",
		context,
		["context_code", "context_name", "context_status", "context_discoverability"],
		as_dict=True,
	)
	if not found:
		return {"exists": False, "problem": None}
	out = {"exists": True, "code": found.context_code, "name": found.context_name, "problem": None}
	other = frappe.db.get_value(
		constants.RUN,
		{
			"context_id": context,
			# a proposal holds nothing: several may wait for one delegation, and one is approved
			"state": ["not in", (constants.DISCARDED, constants.REJECTED, constants.PROPOSED)],
			"name": ["!=", run or ""],
		},
		"display_id",
	)
	if (found.context_status, found.context_discoverability) != ("Locked", "Hidden"):
		out["problem"] = _("The delegation's context is not locked and hidden")
	elif other:
		out["problem"] = _("Run {0} already holds this delegation").format(other)
	else:
		for doctype in ORDER:
			where = selection(doctype)
			# nosemgrep: frappe-semgrep-rules.rules.security.frappe-sql-format-injection -- reviewed: the DocType is one of ORDER and the condition is built by selection() from constants; the context is a parameter
			if frappe.db.sql(
				f"select 1 from `tab{doctype}` r {where} limit 1", (context,) * where.count("%s")
			):
				out["problem"] = _("The delegation's context already holds {0} records").format(_(doctype))
				break
	return out


def _staged_before(run, manifest, dest):
	"""The run this publication already has on the site, where it was staged before. A file that was
	refused before is checked again from the start, and takes over the refusal's record."""
	if not frappe.db.get_value(constants.RUN, run, "name", for_update=True):
		return None
	record = runs.record(run)
	if json.loads(record.manifest_json) != manifest or record.state == constants.DISCARDED:
		raise ValueError("Existing run cannot receive this publication")
	if record.state in (constants.REJECTED, constants.AWAITING, constants.PROPOSED):
		return None
	if record.state != constants.ACTIVATED:  # an activated run's file is gone, and nothing reads it again
		verify_publication(dest, run)
	return record


def _keep_run(run, values):
	"""Insert a run under its publication's identity, or take over the record a refusal left."""
	if frappe.db.exists(constants.RUN, run):
		record = frappe.get_doc(constants.RUN, run, for_update=True)
		record.update(values)
		record.save(ignore_permissions=True)
	else:
		record = frappe.get_doc({"doctype": constants.RUN, **values})
		record.insert(set_name=run, ignore_permissions=True)
	return record


def _refuse(run, manifest, inventory, seconds):
	"""Keep a refused publication as a run the migration team can open, holding what differs and
	nothing else: no file is kept and no row is written. Counts and names only, as the preflight."""
	from .ucm_migration_loader import run_overview

	reasons = [reason for key, reason in REFUSALS if inventory[key]]
	_keep_run(
		run,
		{
			"context_id": manifest["context"],
			"snapshot_id": manifest["snapshot"],
			"manifest_json": encoded(manifest).decode(),
			"state": constants.REJECTED,
			"received": manifest["batches"],
			"applied": 0,
			"document_count": sum(manifest["counts"].values()),
			"finding": "Preflight refused: " + ", ".join(reasons),
			"reconciliation_json": json.dumps({"preflight": inventory}),
			"timeline_json": None,
		},
	)
	run_overview.staged(run, seconds, 0)
	# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: the refusal is kept although the staging request fails; nothing else was written in this transaction
	frappe.db.commit()


def stage(path: str, run: str):
	"""Bench operator entry point. Register only a verified private copy on this site."""
	runs.only_admin()
	started = monotonic()
	dest = local_publication(run)
	dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
	with tempfile.TemporaryDirectory(dir=dest.parent) as work:
		proposed = Path(work) / "publication"
		with Path(path).open("rb") as source, proposed.open("xb") as output:
			os.fchmod(output.fileno(), 0o600)
			shutil.copyfileobj(source, output)
			output.flush()
			os.fsync(output.fileno())
		manifest = verify_publication(proposed, run)
		if manifest["site"] != frappe.local.site:
			raise ValueError("Publication targets another site")
		context = manifest["context"]
		frappe.db.get_value("Context", context, "name", for_update=True)
		record = _staged_before(run, manifest, dest)
		if record:
			return runs.result(record)
		if "Organization" not in manifest["counts"] or "sensitive_field_policy" not in manifest:
			raise ValueError(
				"Freeze a new publication including organizations and the sensitive field policy"
			)
		with open_sealed(proposed, immutable=True, one_view=False) as db:
			inventory = _inventory(db, run, manifest)
		if inventory["schema_differences"] is None:
			raise ValueError("Create a new sealed publication with the publisher schema receipt")
		if (
			inventory["schema_differences"]
			or inventory["release_differences"]
			or inventory["existing_keys"]
			or inventory["existing_share_receipts"]
		):
			_refuse(run, manifest, inventory, monotonic() - started)
			raise ValueError(f"Publication preflight failed: {inventory}")
		from .runs import check_closed

		ctx = frappe.get_doc("Context", context, for_update=True)
		check_closed(ctx)
		# a refusal the approval's dialog showed in the same words; the context is locked from here on
		if problem := context_state(context, run)["problem"]:
			raise ValueError(problem)
		if dest.exists():
			# A previous process may have died after the link and before database commit.
			verify_publication(dest, run)
		else:
			os.replace(proposed, dest)
			fd = os.open(dest.parent, os.O_RDONLY)
			try:
				os.fsync(fd)
			finally:
				os.close(fd)
		from .ucm_migration_loader import run_overview
		from .ucm_migration_loader.set_based import _business_batches
		from .write_routes import default_plan

		with open_sealed(dest, immutable=True, one_view=False) as db:
			stamp = publication_index.build_index(db, dest, manifest)
			business = _business_batches(db)
		record = _keep_run(
			run,
			{
				"context_id": context,
				"snapshot_id": manifest["snapshot"],
				"manifest_json": encoded(manifest).decode(),
				"write_plan_json": json.dumps(default_plan(manifest)),
				"state": constants.STAGED,
				"publication_stamp": stamp,
				"received": manifest["batches"],
				"applied": 0,
				"document_count": sum(manifest["counts"].values()),
				"finding": None,
				"reconciliation_json": None,
				"timeline_json": None,
			},
		)
		# the form's step list: how long the check and staging took, and where the business batches end
		run_overview.staged(run, monotonic() - started, business)
		return runs.result(record)


@contextmanager
def indexed_publication(run, initial):
	"""Open a checked receiver snapshot while the caller holds the run lock."""
	path = staged_file(run)
	stamp = json.loads(initial.publication_stamp or "null")
	if stamp:
		publication_index.check_identity(path, stamp)
	opened_identity = publication_index.identity(path)
	with open_sealed(path, immutable=True) as db:
		manifest = json.loads(initial.manifest_json)
		if not stamp:
			# Legacy publications get a trusted index only after full verification.
			if verify_connection(db, run) != manifest:
				raise ValueError("Legacy publication differs from receiver manifest")
			if publication_index.identity(path) != opened_identity:
				raise ValueError("Publication changed during verification")
			value = publication_index.build_index(db, path, manifest)
			frappe.db.set_value(constants.RUN, run, "publication_stamp", value)
			stamp = json.loads(value)
		if opened_identity != stamp[0]:
			raise ValueError("Publication changed while opening snapshot")
		publication_index.check_identity(path, stamp)
		with publication_index.index_path(path).open("rb") as hashes:
			yield db, hashes, manifest, path, stamp


def rebuild_index(run: str):
	"""Restore an index after a site restore; require the entire file to verify first."""
	runs.only_admin()
	from .runs import lock_run

	_, record = lock_run(run)
	path = staged_file(run)
	original = publication_index.identity(path)
	with open_sealed(path, immutable=True) as db:
		manifest = verify_connection(db, run)
		if manifest != json.loads(record.manifest_json) or manifest["site"] != frappe.local.site:
			raise ValueError("Publication differs from the staged run")
		if publication_index.identity(path) != original:
			raise ValueError("Publication changed while verifying restored file")
		stamp = publication_index.build_index(db, path, manifest)
		frappe.db.set_value(constants.RUN, run, "publication_stamp", stamp)
	return {"run": run, "verified_batches": manifest["batches"]}


def remove_staged_file(run: str) -> None:
	"""Once the transaction is committed, remove the run's staged file and its index: a copy of the delegation
	the site no longer needs. An activated run has kept what it reads later (the shares it holds), and its
	approval stays, so that the publishing side still learns its file arrived."""
	path = local_publication(run)
	frappe.db.after_commit.add(lambda: path.unlink(missing_ok=True))
	frappe.db.after_commit.add(lambda: publication_index.index_path(path).unlink(missing_ok=True))


def remove_after_discard(record):
	import shutil

	arrived = inbox(record.name)
	remove_staged_file(record.name)
	# with the approval: no file is accepted for a discarded run
	frappe.db.after_commit.add(lambda: shutil.rmtree(arrived, ignore_errors=True))
