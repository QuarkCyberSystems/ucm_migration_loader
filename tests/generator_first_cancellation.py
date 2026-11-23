"""Cancel the first partially committed generator wave, replace it, and open the empty site."""

import argparse
import json
import os
import tempfile
from pathlib import Path

import frappe
import generator_catalogs
import generator_countries
import generator_geography
import generator_places
import generator_target
from generator_cleanup import cold_discard
from generator_collisions import read_site
from generator_committed_waves import fixture, invoke
from generator_snapshot import BENCH, site_snapshot
from generator_transfer import deliver
from ucm_migration_loader.frozen import freeze


def exercise(source, site, work, prepared=False):
    assert (
        site.startswith("ucm-full-wave-")
        and site.endswith(".localhost")
        and source != site
    )
    initial = (
        {"site": site, "resumed_staged_publication": True}
        if prepared
        else generator_target.prepare(site)
    )
    os.chdir(BENCH / "sites")
    if not prepared:
        generator_countries.exercise(source, site)
        generator_geography.rehearse(source, site)
        generator_catalogs.exercise(source, site)
        generator_places.exercise(source, site)
    with read_site(source) as db:
        with db.cursor() as cur:
            cur.execute("select name from tabContext where context_code='BOL'")
            (context,) = cur.fetchone()

        def publish(label):
            return freeze(
                db,
                work / label,
                (BENCH / "sites" / source / "private/files").resolve(),
                site,
                label,
                context,
                25,
                262144,
            )

        first = publish("initial")
        replacement = publish("replacement")
    assert (
        first["run"] != replacement["run"]
        and first["batch_chain_sha256"] == replacement["batch_chain_sha256"]
    )
    assert (
        first["application_versions"]["ucm"] and first["referenced_dictionary_sha256"]
    )
    frappe.init(site=site)
    frappe.connect()
    try:
        assert not frappe.db.count("Person") and not frappe.db.count("Organization")
        baseline = site_snapshot([context])
        first_run = first["run"]
        if prepared:
            existing = frappe.get_doc("UCM Migration Run", first_run)
            assert existing.applied == 0 and existing.state == "Publication staged"
        else:
            deliver(site, work / "initial", first_run, work)
        fixture(site, first_run)
        partial = invoke(site, "apply", first_run, max_batches=2)
        assert partial["applied"] == 2
        frappe.db.rollback()
        assert frappe.db.count("Organization", {"context_tech_id": context}) > 0
        assert frappe.db.get_value("Context", context, "context_status") == "Locked"
        one = cold_discard(site, first_run, 1)
        cancelled = cold_discard(site, first_run, 2)
        assert (
            one["applied"] == 1
            and cancelled["state"] == "Discarded"
            and cancelled["applied"] == 0
        )
        assert site_snapshot([context]) == baseline
        frappe.db.rollback()
        assert frappe.db.count("UCM Migration Batch", {"migration_run": first_run}) == 2
        run = replacement["run"]
        delivered = deliver(site, work / "replacement", run, work)
        ready = invoke(site, "apply", run, max_batches=replacement["batches"] + 1)
        assert ready["state"] == "Publication ready"
        active = invoke(site, "activate", run)
        assert (
            active["state"] == "Activated"
            and active["applied"] == replacement["batches"]
        )
        frappe.db.rollback()
        rec = json.loads(
            frappe.db.get_value("UCM Migration Run", run, "reconciliation_json")
        )
        stored = site_snapshot([context])["waves"][context]["tables"]
        expected = replacement["counts"].copy()
        expected["Record Share"] -= rec["shares_held_at_activation"]
        assert {dt: r["rows"] for dt, r in stored.items()} == expected
        return {
            "site": site,
            "initial": initial,
            "partial": partial,
            "cleanup_checkpoint": one,
            "cancelled": cancelled,
            "all_tables_restored_to_empty_baseline": True,
            "original_receipts_retained": 2,
            "replacement": active,
            "restored_rows_verified": True,
            "observed_sensitive_rows": rec["sensitive_records_by_owner_type"],
            "held_historical_shares": rec["shares_held_at_activation"],
            "handoff_tampering_rejected": delivered["changed_handoff_refused"],
            "application_versions": replacement["application_versions"],
            "referenced_dictionary_sha256": replacement["referenced_dictionary_sha256"],
        }
    finally:
        frappe.destroy()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-site", required=True)
    p.add_argument("--fresh-site", required=True)
    p.add_argument("--evidence", required=True, type=Path)
    p.add_argument("--resume-staged", action="store_true")
    args = p.parse_args()
    output = args.evidence.resolve()
    assert not output.exists()
    with tempfile.TemporaryDirectory() as tmp:
        result = exercise(
            args.source_site, args.fresh_site, Path(tmp), args.resume_staged
        )
    with output.open("x") as handle:
        json.dump(result, handle, indent=2)
    print(json.dumps(result))
