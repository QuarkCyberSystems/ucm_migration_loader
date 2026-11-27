"""Receive a sealed publication on the bench and bind it to a locked target context."""

import json
import os
import re
import shutil
import sqlite3
import tempfile
from collections import Counter, defaultdict
from contextlib import closing, contextmanager
from pathlib import Path

import frappe

from . import api, publication_index
from .contract import encoded, table_signatures
from .frozen import (
    CHILDREN,
    FORMAT,
    ORDER,
    TABLES,
    application_versions,
    selection,
    verify_connection,
    verify_publication,
)


def local_publication(run):
    if not re.fullmatch(r"[a-f0-9]{64}", run):
        raise ValueError("Invalid publication run ID")
    return Path(
        frappe.get_site_path("private", "migration-publications", run + ".sqlite")
    )


def schema_differences(manifest):
    if "schema" not in manifest:
        return None  # Earlier sealed files predate the schema receipt.
    algorithm = manifest.get("schema_algorithm")
    if algorithm not in (None, "sorted-index-definitions-v1"):
        raise ValueError("Unsupported publication schema fingerprint")
    current = table_signatures(
        lambda sql: frappe.db.sql(sql)[0][1],
        [
            dt
            for dt in TABLES
            if dt != "Organization" or "Organization" in manifest["counts"]
        ],
        sort_indexes=algorithm is not None,
    )
    return sorted(
        dt
        for dt in set(current) | set(manifest["schema"])
        if current.get(dt) != manifest["schema"].get(dt)
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
    api._operator()
    uri = Path(path).resolve(strict=True).as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.execute("pragma trusted_schema=OFF")
        db.execute("begin")
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


def stage(path: str, run: str):
    """Bench operator entry point. Register only a verified private copy on this site."""
    api._operator()
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
        if frappe.db.get_value(api.RUN, run, "name", for_update=True):
            record = api._record(run)
            if (
                json.loads(record.manifest_json) != manifest
                or record.state == "Discarded"
            ):
                raise ValueError("Existing run cannot receive this publication")
            verify_publication(dest, run)
            return api._result(record)
        if (
            "Organization" not in manifest["counts"]
            or "sensitive_field_policy" not in manifest
        ):
            raise ValueError(
                "Freeze a new publication including organizations and the sensitive field policy"
            )
        with closing(
            sqlite3.connect(
                proposed.resolve().as_uri() + "?mode=ro&immutable=1", uri=True
            )
        ) as db:
            db.execute("pragma trusted_schema=OFF")
            inventory = _inventory(db, run, manifest)
        if inventory["schema_differences"] is None:
            raise ValueError(
                "Create a new sealed publication with the publisher schema receipt"
            )
        if (
            inventory["schema_differences"]
            or inventory["release_differences"]
            or inventory["existing_keys"]
            or inventory["existing_share_receipts"]
        ):
            raise ValueError(f"Publication preflight failed: {inventory}")
        from .application import check_closed

        ctx = frappe.get_doc("Context", context, for_update=True)
        check_closed(ctx, pilot=False)
        for dt in ORDER:
            where = selection(dt)
            if frappe.db.sql(
                f"select 1 from `tab{dt}` r {where} limit 1",
                (context,) * where.count("%s"),
            ):
                raise ValueError(f"Incoming delegation already contains {dt} records")
        if frappe.db.get_value(
            api.RUN,
            {"context_id": context, "state": ["!=", "Discarded"]},
            "name",
            for_update=True,
        ):
            raise ValueError("Another run already owns this context")
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
        from .write_routes import default_plan

        with closing(
            sqlite3.connect(dest.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        ) as db:
            db.execute("pragma trusted_schema=OFF")
            stamp = publication_index.build_index(db, dest, manifest)
        record = frappe.get_doc(
            {
                "doctype": api.RUN,
                "context_id": context,
                "snapshot_id": manifest["snapshot"],
                "manifest_json": encoded(manifest).decode(),
                "write_plan_json": json.dumps(default_plan(manifest)),
                "state": "Publication staged",
                "publication_stamp": stamp,
                "received": manifest["batches"],
                "applied": 0,
                "document_count": sum(manifest["counts"].values()),
            }
        )
        record.insert(set_name=run, ignore_permissions=True)
        return api._result(record)


@contextmanager
def indexed_publication(run, initial):
    """Open a checked receiver snapshot while the caller holds the run lock."""
    path = local_publication(run).resolve(strict=True)
    stamp = json.loads(initial.publication_stamp or "null")
    if stamp:
        publication_index.check_identity(path, stamp)
    opened_identity = publication_index.identity(path)
    uri = path.as_uri() + "?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.execute("pragma trusted_schema=OFF")
        db.execute("begin")
        manifest = json.loads(initial.manifest_json)
        if not stamp:
            # Legacy publications get a trusted index only after full verification.
            if verify_connection(db, run) != manifest:
                raise ValueError("Legacy publication differs from receiver manifest")
            if publication_index.identity(path) != opened_identity:
                raise ValueError("Publication changed during verification")
            value = publication_index.build_index(db, path, manifest)
            frappe.db.set_value(api.RUN, run, "publication_stamp", value)
            stamp = json.loads(value)
        if opened_identity != stamp[0]:
            raise ValueError("Publication changed while opening snapshot")
        publication_index.check_identity(path, stamp)
        with publication_index.index_path(path).open("rb") as hashes:
            yield db, hashes, manifest, path, stamp


def rebuild_index(run: str):
    """Restore an index after a site restore; require the entire file to verify first."""
    api._operator()
    from .application import lock_run

    _, record = lock_run(run)
    path = local_publication(run).resolve(strict=True)
    original = publication_index.identity(path)
    with closing(
        sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    ) as db:
        db.execute("pragma trusted_schema=OFF")
        db.execute("begin")
        manifest = verify_connection(db, run)
        if (
            manifest != json.loads(record.manifest_json)
            or manifest["site"] != frappe.local.site
        ):
            raise ValueError("Publication differs from the staged run")
        if publication_index.identity(path) != original:
            raise ValueError("Publication changed while verifying restored file")
        stamp = publication_index.build_index(db, path, manifest)
        frappe.db.set_value(api.RUN, run, "publication_stamp", stamp)
    return {"run": run, "verified_batches": manifest["batches"]}


def remove_after_discard(record):
    if json.loads(record.manifest_json).get("format") == FORMAT:
        path = local_publication(record.name)
        frappe.db.after_commit.add(lambda: path.unlink(missing_ok=True))
        frappe.db.after_commit.add(
            lambda: publication_index.index_path(path).unlink(missing_ok=True)
        )
