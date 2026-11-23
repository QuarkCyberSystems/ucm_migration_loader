"""Provision a fresh target and qualify both generator waves with an independent live caseworker."""

import argparse
import json
import os
import tempfile
from pathlib import Path
from time import monotonic

import frappe
import generator_catalogs
import generator_committed_waves
import generator_countries
import generator_freeze
import generator_geography
import generator_places
import generator_target
from generator_snapshot import BENCH
from generator_transfer import deliver

from ucm_migration_loader.publication_worker import apply


def validate_staged_guards(run):
    record = frappe.get_doc("UCM Migration Run", run)
    assert record.state == "Publication staged" and not record.applied
    ctx = frappe.get_doc("Context", record.context_id)
    try:
        ctx.context_status = "Active"
        ctx.save()
    except frappe.ValidationError as exc:
        assert "Activate or discard" in str(exc)
    else:
        raise AssertionError("Manual activation opened a staged context")
    finally:
        frappe.db.rollback()
    exemption = frappe.db.get_value(
        "Context Profile Exemption", {"context_tech_id": ctx.name}, "name"
    )
    assert exemption
    try:
        # Change only this connection's transaction. A failed attempt must restore it.
        frappe.db.delete("Context Profile Exemption", {"name": exemption})
        apply(run)
    except ValueError as exc:
        assert "context exemptions differ" in str(exc)
    else:
        raise AssertionError(
            "A changed target exemption reached the first business insert"
        )
    finally:
        frappe.db.rollback()
    assert frappe.db.exists("Context Profile Exemption", exemption)
    assert not frappe.db.count("UCM Migration Batch", {"migration_run": run})


def run(
    source,
    site,
    work,
    snapshot_label="migration-access-240",
    caseworkers=1,
    test_held_shares=False,
    mixed_routes=False,
):
    if source == site or not site.startswith("ucm-full-wave-"):
        raise ValueError("Select a fresh, isolated full-wave rehearsal site")
    target = generator_target.prepare(site)
    os.chdir(BENCH / "sites")
    generator_countries.exercise(source, site)
    generator_geography.rehearse(source, site)
    generator_catalogs.exercise(source, site)
    generator_places.exercise(source, site)
    frappe.init(site=source)
    frappe.connect()
    try:
        started = monotonic()
        frozen = generator_freeze.exercise(
            source, site, work / "sealed", snapshot_label
        )
    finally:
        frappe.destroy()
    frappe.init(site=site)
    frappe.connect()
    try:
        transfers = {}
        for code in ("BOL", "CHA"):
            transfers[code] = deliver(
                site, work / "sealed" / (code + ".sqlite"), frozen[code]["run"], work
            )
            assert transfers[code]["received"]["applied"] == 0
        if mixed_routes:
            from ucm_migration_loader import write_routes

            plans = {}
            for code in ("BOL", "CHA"):
                run_id = frozen[code]["run"]
                rows = write_routes.options(run_id)
                assert all(row["sql_allowed"] for row in rows)
                plan = {
                    row["doctype"]: write_routes.SQL
                    if code == "BOL" or row["doctype"] == "Person"
                    else write_routes.DOCUMENT
                    for row in rows
                }
                plans[run_id] = plan
                write_routes.configure(run_id, plan)
            frappe.db.commit()
        validate_staged_guards(frozen["CHA"]["run"])
        try:
            apply(frozen["BOL"]["run"])
        except ValueError as exc:
            if "Unresolved reference for User" not in str(exc):
                raise
        else:
            raise AssertionError(
                "Expected missing historical users to stop the first batch"
            )
        assert not frappe.db.count("Person")
        assert not frappe.db.count("UCM Migration Batch")
        measured = generator_committed_waves.exercise(
            site,
            frozen["BOL"]["run"],
            frozen["CHA"]["run"],
            work,
            caseworkers,
            test_held_shares,
        )
        if mixed_routes:
            for wave in (frozen["BOL"], frozen["CHA"]):
                receipts = frappe.get_all(
                    "UCM Migration Batch",
                    filters={"migration_run": wave["run"]},
                    fields=["parent_doctype", "write_route"],
                )
                plan = plans[wave["run"]]
                assert {r.parent_doctype for r in receipts} == set(plan)
                assert all(r.write_route == plan[r.parent_doctype] for r in receipts)
                try:
                    write_routes.configure(wave["run"], {})
                except ValueError as exc:
                    assert "locked" in str(exc)
                else:
                    raise AssertionError("Routes were editable after apply")
            measured["write_plans"] = plans
        if test_held_shares:
            from generator_organization_access import exercise as organization_access

            measured["access_checks"]["organizations"] = organization_access(work)
        return {
            "initial": target,
            "snapshot_label": snapshot_label,
            "freeze_through_observed_release_seconds": round(monotonic() - started, 2),
            "publication": frozen,
            "transfers": transfers,
            "missing_actor_stopped_before_writes": True,
            "missing_exemption_stopped_before_writes": True,
            "manual_release_of_staged_context_blocked": True,
            **measured,
        }
    finally:
        frappe.destroy()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-site", required=True)
    parser.add_argument("--fresh-site", required=True)
    parser.add_argument("--snapshot-label", default="migration-access-240")
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--caseworkers", type=int, default=1)
    parser.add_argument("--test-held-shares", action="store_true")
    parser.add_argument("--mixed-routes", action="store_true")
    args = parser.parse_args()
    evidence = args.evidence.resolve()
    assert not evidence.exists()
    with tempfile.TemporaryDirectory() as work:
        result = run(
            args.source_site,
            args.fresh_site,
            Path(work),
            args.snapshot_label,
            args.caseworkers,
            args.test_held_shares,
            args.mixed_routes,
        )
    with evidence.open("x") as out:
        json.dump(result, out, indent=2)
    print(json.dumps(result))
