"""Publish the cancelled delegation again via HTTPS and exercise queued operator commands."""

import argparse
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import frappe
from first_wave import caseworker_session
from generator_collisions import read_site
from generator_snapshot import BENCH, site_snapshot
from generator_transfer import deliver
from rehearse import call, worker
from ucm_migration_loader.frozen import freeze, verify_publication
from ucm_migration_loader.held_shares import HELD
from ucm_migration_loader.publication_receiver import local_publication


@contextmanager
def operator_session(work):
    from ucm.tests.utils import make_user

    operator = make_user(f"release-{uuid4().hex}@example.invalid", [])
    account = frappe.get_doc("User", operator)
    try:
        account.add_roles("System Manager")
        frappe.db.commit()
        with caseworker_session(operator, work) as request:

            def command(method, **data):
                return request(
                    "POST", "/api/method/ucm_migration_loader.api." + method, json=data
                )["message"]

            yield operator, command
    finally:
        frappe.db.rollback()
        account = frappe.get_doc("User", operator)
        account.remove_roles("System Manager")
        account.enabled = 0
        account.save()
        frappe.db.commit()


def queue_state(command, run, expected):
    worker()
    status = command("status", run=run)
    if status["state"] != expected or status["finding"]:
        raise ValueError(f"Unexpected migration job result: {status}")
    return status


def exercise(site, source, work, previous, resume_run=None):
    with read_site(source) as db:
        with db.cursor() as cur:
            cur.execute("select name from tabContext where context_code='CHA'")
            (context,) = cur.fetchone()
        if not resume_run:
            pub = freeze(
                db,
                work / "CHA.sqlite",
                (BENCH / "sites" / source / "private/files").resolve(),
                site,
                "migration-access-240-reload",
                context,
                25,
                256 * 1024,
            )
    frappe.init(site=site)
    frappe.connect()
    try:
        if resume_run:
            run = resume_run
            pub = verify_publication(local_publication(run), run)
            assert (
                pub["context"] == context
                and pub["site"] == site
                and pub["snapshot"] == "migration-access-240-reload"
            )
        else:
            run = pub["run"]
        cancelled = call("status", run=previous)
        assert cancelled["state"] == "Discarded" and cancelled["applied"] == 0
        a = frappe.db.get_value("Context", {"context_code": "BOL"}, "name")
        before = site_snapshot([a])

        def shares():
            return {
                r.name: r
                for r in frappe.get_all(
                    "Record Share", filters={"context_tech_id": a}, fields=["*"]
                )
            }

        previous_shares = shares()
        previous_holds = set(
            frappe.get_all(
                HELD,
                filters={
                    "owner_context": a,
                    "receiver_context": context,
                    "state": "Held",
                },
                pluck="name",
            )
        )
        if resume_run:
            start = call("status", run=run)
            assert start["state"] == "Publication applying"
            transfer = {"existing_receipt": start}
            limit = 1
        else:
            transfer = deliver(site, work / "CHA.sqlite", run, work)
            start = transfer["received"]
            assert start["applied"] == 0
            limit = 2
        stop_at = start["applied"] + limit
        with operator_session(work) as (operator, command):
            command("apply", run=run, max_batches=limit)
            command("pause", run=run)
            paused = queue_state(command, run, "Publication paused")
            assert paused["applied"] == start["applied"]
            command("apply", run=run, max_batches=limit)
            bounded = queue_state(command, run, "Publication applying")
            assert bounded["applied"] == stop_at
            from ucm_migration_loader.publication_jobs import execute

            ticket = frappe.db.get_value("UCM Migration Run", run, "dispatch_id")
            execute(run, "apply", ticket, limit, stop_at=stop_at)
            frappe.db.rollback()
            assert command("status", run=run)["applied"] == stop_at
            command("apply", run=run)
            ready = queue_state(command, run, "Publication ready")
            report = command("report", run=run)["reconciliation"]
            assert sum(report["published_rows"].values()) == ready["documents"]
            command("activate", run=run)
            done = queue_state(command, run, "Activated")
            assert command("report", run=run)["activated_by"] == operator
        assert done["applied"] == done["received"]
        after = site_snapshot([a])
        released = set(
            frappe.get_all(
                HELD,
                filters={
                    "owner_context": a,
                    "receiver_context": context,
                    "state": "Released",
                    "release_run": run,
                },
                pluck="name",
            )
        )
        assert released <= previous_holds
        current_shares = shares()
        assert {
            name: current_shares[name] for name in previous_shares
        } == previous_shares
        assert current_shares.keys() - previous_shares.keys() == released
        for view in (before, after):
            del view["waves"][a]["tables"]["Record Share"]
        assert before == after
        counts = site_snapshot([context])["waves"][context]["tables"]
        assert {dt: v["rows"] for dt, v in counts.items()} == pub["counts"]
        return {
            "site": site,
            "previous": cancelled,
            "transfer": transfer,
            "pause": paused,
            "bounded_resume": bounded,
            "ready": ready,
            "activated": done,
            "reconciliation": report,
            "existing_live_rows_unchanged": True,
            "newly_released_historical_shares": len(released),
            "authenticated_release_operator_recorded": True,
            "bounded_job_replay_kept_cursor": True,
        }
    finally:
        frappe.destroy()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for field in ("site", "source-site", "previous-run", "evidence"):
        p.add_argument("--" + field, required=True)
    p.add_argument("--resume-run")
    args = p.parse_args()
    out = Path(args.evidence).resolve()
    assert (
        args.site.startswith("ucm-full-wave-")
        and args.site.endswith(".localhost")
        and not out.exists()
    )
    os.chdir(BENCH / "sites")
    with tempfile.TemporaryDirectory() as tmp:
        result = exercise(
            args.site, args.source_site, Path(tmp), args.previous_run, args.resume_run
        )
    with out.open("x") as handle:
        json.dump(result, handle, indent=2)
