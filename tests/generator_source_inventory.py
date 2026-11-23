"""Check source inventory through the standalone publisher and exercise linked exceptions."""

import argparse
import getpass
import json
import os
import subprocess
import tempfile
from pathlib import Path

import frappe
import pymysql
from generator_snapshot import BENCH, site_connection
from transport import publication_reader
from ucm_migration_loader.frozen import freeze
from ucm_migration_loader.source_inventory import inventory


def exercise(site, work):
    assert site.startswith("ucm-generator-") and site.endswith(".localhost")
    cfg = frappe.conf
    waves = {}
    with (
        pymysql.connect(unix_socket=cfg.db_socket, user=getpass.getuser()) as admin,
        admin.cursor() as cur,
        publication_reader(cur, cfg.db_name, cfg.db_socket, work) as config,
    ):
        for context, code in frappe.db.sql(
            "select name,context_code from tabContext order by context_code"
        ):
            cmd = [
                str(BENCH / "env/bin/ucm-push"),
                "source-inventory",
                "--site",
                "ucm.localhost",
                "--context",
                context,
                "--snapshot",
                site,
                "--source-config",
                str(config),
            ]
            report = json.loads(subprocess.check_output(cmd, text=True))
            assert not report["unsupported_business_rows"]
            assert report["rows_with_context_column"][
                "Organization"
            ] == frappe.db.count("Organization", {"context_tech_id": context})
            waves[code] = report
    with site_connection() as conn, conn.cursor() as cur:
        ctx = waves["BOL"]["context"]
        for table, kind, key in (
            ("tabFile", "attached_to_doctype", "attached_to_name"),
            ("tabToDo", "reference_type", "reference_name"),
        ):
            cur.execute(f"create temporary table scope_fixture like `{table}`")
            cur.execute(f"insert into scope_fixture select * from `{table}` limit 1")
            cur.execute(f"alter table scope_fixture rename to `{table}`")
            cur.execute(
                f"update `{table}` set `{kind}`='Activity', `{key}`=(select name from tabActivity where context_tech_id=%s limit 1)",
                ctx,
            )
        bol = inventory(conn, ctx)["unsupported_business_rows"]
        cha = inventory(conn, waves["CHA"]["context"])["unsupported_business_rows"]
        assert bol["File on Activity"] == bol["ToDo on Activity"] == 1
        assert "File on Activity" not in cha and "ToDo on Activity" not in cha
        output = work / "unsupported.sqlite"
        try:
            freeze(conn, output, None, "ucm.localhost", "scope-test", ctx, 25, 262144)
        except ValueError as exc:
            assert "File on Activity" in str(exc) and "ToDo on Activity" in str(exc)
        else:
            raise AssertionError("Unsupported linked rows did not stop freeze")
        assert not output.exists()

    return {
        "source": site,
        "waves": waves,
        "refused_before_publication": True,
        "linked_file_and_task_scoped_to_owner": True,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-site", required=True)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    output = args.evidence.resolve()
    os.chdir(BENCH / "sites")
    frappe.init(site=args.source_site)
    frappe.connect()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = exercise(args.source_site, Path(tmp))
        with output.open("x") as out:
            json.dump(result, out, indent=2)
        print(
            json.dumps(
                {
                    code: r["unsupported_business_rows"]
                    for code, r in result["waves"].items()
                }
            )
        )
    finally:
        frappe.destroy()
