"""Run from the bench root. Creates identifiable synthetic pilot contexts and persistent audit receipts."""

import argparse
import json
import os
import subprocess
from uuid import uuid4

import frappe
import pymysql
from rehearsal_bench import BENCH

from ucm_migration_loader import api
from ucm_migration_loader.contract import digest, plan
from ucm_migration_loader.push import publication, send


def insert_fixture(cursor, ids, context):
    cursor.execute(
        "insert into tabPerson (name, context_tech_id, person_name, person_file_id) values (%s,%s,%s,%s)",
        (ids["person"], context, "Pilot Person", "SYN-" + ids["person"][:8]),
    )
    cursor.execute(
        "insert into tabCase (name, context_tech_id, case_name, case_number, case_status) values (%s,%s,%s,%s,%s)",
        (ids["case"], context, "Pilot Case", "SYN-" + ids["case"][:8], "Open"),
    )
    cursor.execute(
        """insert into `tabPerson Other Name` (name,parent,parenttype,parentfield,idx,person_other_name)
        values (%s,%s,'Person','person_other_names',1,'Pilot alias')""",
        (ids["other"], ids["person"]),
    )
    cursor.execute(
        """insert into `tabCase Member` (name,case_tech_id,person_tech_id,person_member_sequence_number)
        values (%s,%s,%s,1)""",
        (ids["member"], ids["case"], ids["person"]),
    )


def synthetic_snapshot(site, context, snapshot):
    cfg = frappe.conf
    ids = {key: str(uuid4()) for key in ("person", "case", "member", "other")}
    with pymysql.connect(
        host=cfg.db_host,
        port=int(cfg.db_port or 3306),
        user=cfg.db_user or cfg.db_name,
        password=cfg.db_password,
        database=cfg.db_name,
    ) as conn:
        with conn.cursor() as cursor:
            for table in (
                "tabPerson",
                "tabCase",
                "tabCase Member",
                "tabPerson Other Name",
            ):
                # Session-local publishing tables preserve native columns. InnoDB disallows FULLTEXT on temporary tables.
                cursor.execute(f"show create table `{table}`")
                ddl = cursor.fetchone()[1].replace(
                    "CREATE TABLE", "CREATE TEMPORARY TABLE", 1
                )
                ddl = "\n".join(
                    line
                    for line in ddl.splitlines()
                    if not line.lstrip().startswith("FULLTEXT KEY")
                )
                cursor.execute(ddl.replace(",\n)", "\n)"))
            insert_fixture(cursor, ids, context)
        manifest, batches = plan(
            site, snapshot, context, publication(conn, context), size=1
        )
    return ids, manifest, batches


def call(method, **params):
    try:
        result = getattr(api, method)(**params)
        frappe.db.commit()
        return result
    except Exception:
        frappe.db.rollback()
        raise


def worker():
    frappe.db.commit()
    subprocess.run(
        ["bench", "worker", "--queue", api.QUEUE, "--burst", "--quiet"], check=True
    )
    frappe.db.rollback()


def new_context(snapshot):
    return frappe.get_doc(
        {
            "doctype": "Context",
            "context_name": "Synthetic migration " + snapshot,
            "context_code": "MLT" + uuid4().hex[:5].upper(),
            "context_status": "Locked",
            "context_discoverability": "Hidden",
            "context_description": api.PILOT_MARKER,
        }
    ).insert()


def make_wave(site, snapshot):
    ctx = new_context(snapshot)
    fixture = synthetic_snapshot(site, ctx.name, snapshot)
    frappe.db.commit()
    return fixture


def stage(manifest, batches):
    receipt = call("begin", manifest=manifest)
    run = receipt["run"]
    # Lose the first response and replay it from a new transaction.
    first = call("upload", run=run, sequence=1, documents=batches[0])
    assert call("upload", run=run, sequence=1, documents=batches[0]) == first
    assert send(call, manifest, batches)["state"] == "Staged"
    assert frappe.db.count(api.BATCH, {"migration_run": run}) == len(batches)
    return run


def finish(run, ids, apply_batches=False, review=None):
    call("verify", run=run)
    worker()
    state = call("status", run=run)
    assert state["state"] == "Verified", state
    assert not frappe.db.exists("Person", ids["person"])
    if apply_batches:
        call("apply", run=run)
        worker()
        ready = call("status", run=run)
        assert ready["state"] == "Ready" and ready["applied"] == ready["received"], (
            ready
        )
        if review:
            review()
    call("activate", run=run)
    worker()
    state = call("status", run=run)
    assert state["state"] == "Activated", state
    assert (
        frappe.db.get_value("Person", ids["person"], "person_all_names")
        == "Pilot Person\nPilot alias"
    )
    assert (
        str(frappe.db.get_value("Person Other Name", ids["other"], "parent"))
        == ids["person"]
    )
    assert (
        str(frappe.db.get_value("Case Member", ids["member"], "case_tech_id"))
        == ids["case"]
    )
    assert (
        str(frappe.db.get_value("Case Member", ids["member"], "person_tech_id"))
        == ids["person"]
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", required=True)
    args = parser.parse_args()
    os.chdir(BENCH / "sites")
    frappe.init(site=args.site)
    frappe.connect()
    from ucm.tests.utils import make_user

    pilot_user = None
    try:
        ids_a, ma, ba = make_wave(args.site, "synthetic-a")
        run_a = stage(ma, ba)
        finish(run_a, ids_a)
        ids_b, mb, bb = make_wave(args.site, "synthetic-b")
        # Fail the last document after Person, child and Case inserts have executed.
        bad_batches = [
            *bb[:-1],
            [{**bb[-1][0], "person_tech_id": str(uuid4())}],
        ]
        bad = {
            **mb,
            "snapshot": "synthetic-b-invalid",
            "batches": list(map(digest, bad_batches)),
        }
        rejected = stage(bad, bad_batches)
        call("verify", run=rejected)
        worker()
        result = call("status", run=rejected)
        assert result["state"] == "Rejected", result
        assert not frappe.db.exists("Person", ids_b["person"])
        assert not frappe.db.exists("Case", ids_b["case"])
        assert not frappe.db.exists("Person Other Name", ids_b["other"])
        call("discard", run=rejected)
        run_b = stage(mb, bb)
        pilot_user = make_user(
            f"ucm-pilot-{uuid4().hex}@example.invalid", [(ma["context"], "Write")]
        )
        frappe.db.commit()
        frappe.set_user(pilot_user)
        assert frappe.has_permission("Case", "write", ids_a["case"])
        live_case = frappe.get_doc("Case", ids_a["case"])
        live_case.case_remark = "Saved while the second synthetic wave was staged"
        live_case.save()
        frappe.db.commit()
        frappe.set_user("Administrator")
        finish(run_b, ids_b)
        assert not frappe.has_permission("Case", "read", ids_b["case"], user=pilot_user)
        assert (
            frappe.db.get_value("Case", ids_a["case"], "case_remark")
            == live_case.case_remark
        )
        print(
            json.dumps(
                {
                    "site": args.site,
                    "activated": [run_a, run_b],
                    "discarded": rejected,
                    "contexts": [ma["context"], mb["context"]],
                }
            )
        )
    finally:
        if pilot_user:
            frappe.db.rollback()
            frappe.set_user("Administrator")
            user = frappe.get_doc("User", pilot_user)
            user.enabled = 0
            user.save()
            frappe.db.commit()
        frappe.destroy()


if __name__ == "__main__":
    main()
