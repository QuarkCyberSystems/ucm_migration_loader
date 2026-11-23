"""Cancel a full hidden wave with an active neighbour and verify exact source-owned inventories."""

import argparse
import json
import os
import subprocess
from pathlib import Path

import frappe
import generator_catalogs
import generator_countries
import generator_freeze
import generator_geography
import generator_places
from generator_committed_waves import fixture, invoke
from generator_snapshot import BENCH, site_snapshot
from ucm_migration_loader.publication_receiver import local_publication, stage


def cold_discard(site, run, limit):
    cmd = [
        "bench",
        "--site",
        site,
        "execute",
        "ucm_migration_loader.publication_cleanup.discard",
        "--kwargs",
        json.dumps({"run": run, "max_batches": limit}),
    ]
    result = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return json.loads(result.stdout)


def prepare(source, site, work):
    generator_countries.exercise(source, site)
    generator_geography.rehearse(source, site)
    generator_catalogs.exercise(source, site)
    generator_places.exercise(source, site)
    frappe.init(site=source)
    frappe.connect()
    try:
        publications = generator_freeze.exercise(source, site, work / "sealed")
    finally:
        frappe.destroy()
    frappe.init(site=site)
    frappe.connect()
    try:
        for code in ("BOL", "CHA"):
            run = publications[code]["run"]
            stage(str(work / "sealed" / (code + ".sqlite")), run)
            fixture(site, run)
            frappe.db.commit()
        return finish_prepare(
            site, publications["BOL"]["run"], publications["CHA"]["run"]
        )
    finally:
        frappe.destroy()


def finish_prepare(site, first, second):
    from generator_end_to_end import validate_staged_guards

    a, b = (frappe.get_doc("UCM Migration Run", key) for key in (first, second))
    assert frappe.db.get_value("Context", a.context_id, "context_code") == "BOL"
    assert frappe.db.get_value("Context", b.context_id, "context_code") == "CHA"
    if b.state == "Publication staged":
        validate_staged_guards(second)
    if a.state != "Activated":
        invoke(site, "apply", first, max_batches=a.received + 1)
        invoke(site, "activate", first)
    held = invoke(site, "apply", second, max_batches=b.received + 1)
    assert (
        held["applied"] > 1
        and held["pending_activation_from_batch"] == held["applied"] + 1
    )
    return {"first": first, "second": second, "held": held}


def exercise(site, runs):
    first, second = runs["first"], runs["second"]
    a, b = [frappe.get_doc("UCM Migration Run", r).context_id for r in (first, second)]
    before = site_snapshot([a])
    assert frappe.db.get_value("Context", a, "context_status") == "Active"
    one = cold_discard(site, second, 1)
    assert (
        one["state"] == "Publication cleaning"
        and one["applied"] == runs["held"]["applied"] - 1
    )
    done = cold_discard(site, second, one["applied"])
    assert done["state"] == "Discarded" and done["applied"] == 0
    assert cold_discard(site, second, 1) == done
    assert not local_publication(second).exists()
    assert before == site_snapshot([a])
    tables = site_snapshot([b])["waves"][b]["tables"]
    assert not any(t["rows"] for t in tables.values())
    frappe.db.rollback()
    receipts = frappe.db.count("UCM Migration Batch", {"migration_run": second})
    assert receipts == runs["held"]["applied"]
    return {
        "site": site,
        "before_cleanup": runs["held"],
        "after_one_batch": one,
        "after_restart": done,
        "retained_receipts": receipts,
        "empty_tables": len(tables),
        "live_delegation_snapshot_matches": True,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for arg in ("site", "source-site", "work", "evidence"):
        p.add_argument("--" + arg, required=True)
    p.add_argument("--staged-runs", nargs=2, metavar=("BOL_RUN", "CHA_RUN"))
    args = p.parse_args()
    work, out = Path(args.work).resolve(), Path(args.evidence).resolve()
    assert (
        args.site.startswith("ucm-full-wave-")
        and args.site.endswith(".localhost")
        and not out.exists()
    )
    os.chdir(BENCH / "sites")
    work.mkdir(mode=0o700, parents=True, exist_ok=True)
    checkpoint = work / "prepared.json"
    if not checkpoint.exists():
        if args.staged_runs:
            frappe.init(site=args.site)
            frappe.connect()
            try:
                prepared = finish_prepare(args.site, *args.staged_runs)
            finally:
                frappe.destroy()
        else:
            prepared = prepare(args.source_site, args.site, work)
        checkpoint.write_text(json.dumps(prepared))
    frappe.init(site=args.site)
    frappe.connect()
    try:
        result = exercise(args.site, json.loads(checkpoint.read_text()))
        with out.open("x") as handle:
            json.dump(result, handle, indent=2)
    finally:
        frappe.destroy()
