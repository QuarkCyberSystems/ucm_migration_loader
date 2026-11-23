"""Push the unaltered generator wave to the populated target and verify collision refusal."""

import argparse
import json
import os
import tempfile
from pathlib import Path
from uuid import uuid4

import frappe
from generator_collisions import read_site
from generator_snapshot import BENCH
from generator_transfer import deliver
from ucm_migration_loader.frozen import freeze
from ucm_migration_loader.publication_receiver import preflight
from ucm_migration_loader.publication_transport import folder, progress


def exercise(source, site, work):
    with read_site(source) as conn:
        with conn.cursor() as cur:
            cur.execute("select name from tabContext where context_code='BOL'")
            (context,) = cur.fetchone()
        path = work / "BOL.sqlite"
        pub = freeze(
            conn,
            path,
            (BENCH / "sites" / source / "private/files").resolve(),
            site,
            "collision-rehearsal-" + uuid4().hex,
            context,
            25,
            256 * 1024,
        )
    run = pub["run"]
    frappe.init(site=site)
    frappe.connect()
    try:
        inspection = preflight(str(path), run)
        assert (
            not inspection["schema_differences"]
            and inspection["existing_keys"]["Person"] == pub["counts"]["Person"]
        )
        before = {
            dt: frappe.db.count(dt)
            for dt in (
                "Person",
                "Case",
                "Organization",
                "Sensitive Information",
                "UCM Migration Run",
                "UCM Migration Batch",
            )
        }
        try:
            deliver(site, path, run, work)
        except ValueError as exc:
            if "Publication preflight failed" not in str(exc):
                raise
        else:
            raise AssertionError("Overlapping published identities reached staging")
        frappe.db.rollback()
        assert not frappe.db.exists("UCM Migration Run", run)
        assert before == {dt: frappe.db.count(dt) for dt in before}
        root = folder(run)
        approval = json.loads((root / "approval.json").read_bytes())
        transfer = progress(root, approval)
        assert transfer["offset"] == path.stat().st_size == transfer["size"]
        return {
            "site": site,
            "source": source,
            "context_code": "BOL",
            "preflight": inspection,
            "transferred_bytes": transfer["offset"],
            "refused_before_staging": True,
            "verified_unchanged_tables": sorted(before),
        }
    finally:
        frappe.destroy()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-site", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--evidence", required=True, type=Path)
    args = p.parse_args()
    evidence = args.evidence.resolve()
    assert args.target == "ucm.localhost" and not evidence.exists()
    os.chdir(BENCH / "sites")
    with tempfile.TemporaryDirectory() as tmp:
        result = exercise(args.source_site, args.target, Path(tmp))
    with evidence.open("x") as out:
        json.dump(result, out, indent=2)
