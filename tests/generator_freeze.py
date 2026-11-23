"""Exercise the standalone handoff with a SELECT-only publisher and independent generator counts."""

import argparse
import getpass
import json
import os
import sqlite3
import subprocess
import tempfile
from pathlib import Path

import frappe
import pymysql
from generator_snapshot import BENCH, scalar, site_connection, site_snapshot
from transport import publication_reader


def check_published_rows(path, expected):
    seen = {dt: set() for dt in expected}
    with (
        sqlite3.connect(path) as db,
        site_connection() as source,
        source.cursor(pymysql.cursors.DictCursor) as cur,
    ):
        for (payload,) in db.execute("select payload from batches order by sequence"):
            for document in json.loads(payload):
                groups = {
                    document["doctype"]: [document["row"]],
                    **document["children"],
                    "File": [f["row"] for f in document.get("files", [])],
                }
                for dt, rows in groups.items():
                    assert dt in seen
                    for row in rows:
                        key = row["name"]
                        assert key not in seen[dt]
                        seen[dt].add(key)
                        cur.execute(f"select * from `tab{dt}` where name=%s", key)
                        stored = json.loads(json.dumps(cur.fetchone(), default=scalar))
                        assert stored == row, dt
    assert {dt: len(keys) for dt, keys in seen.items()} == {
        dt: v["rows"] for dt, v in expected.items()
    }


def exercise(
    site, target, work, snapshot_label="migration-access-240", publisher_cli=None
):
    cfg = frappe.conf
    work.mkdir(mode=0o700, exist_ok=False)
    contexts = frappe.get_all(
        "Context", fields=["name", "context_code"], order_by="context_code"
    )
    snapshot = site_snapshot([r.name for r in contexts])
    cli = str(publisher_cli or BENCH / "env/bin/ucm-push")
    files = Path(frappe.get_site_path("private", "files")).resolve()
    results = {}
    with (
        pymysql.connect(
            unix_socket=cfg.db_socket, user=getpass.getuser(), autocommit=True
        ) as admin,
        admin.cursor() as cur,
        tempfile.TemporaryDirectory() as tmp,
        publication_reader(cur, cfg.db_name, cfg.db_socket, Path(tmp)) as config,
    ):
        for ctx in contexts:
            out = work / (ctx.context_code + ".sqlite")
            findings = work / (ctx.context_code + "-findings.jsonl")
            options = [
                cli,
                "freeze",
                "--site",
                target,
                "--context",
                ctx.name,
                "--snapshot",
                snapshot_label,
                "--batch-size",
                "25",
                "--private-files",
                str(files),
                "--source-config",
                str(config),
            ]
            proc = subprocess.run(
                [
                    *options,
                    "--publication-out",
                    str(out),
                    "--findings-out",
                    str(findings),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            missing = frappe.db.sql(
                """select count(*) from tabActivity a
                join `tabActivity Type` t on t.name=a.activity_type_tech_id
                where a.context_tech_id=%s and t.activity_type_enabled_place=1 and t.activity_type_place_required=1
                and a.activity_place_tech_id is null""",
                (ctx.name,),
            )[0][0]
            if missing:
                subject = "activity lacks" if missing == 1 else "activities lack"
                assert (
                    proc.returncode
                    and f"{missing} {subject} a required place" in proc.stderr
                    and not out.exists()
                )
                rows = [json.loads(line) for line in findings.read_text().splitlines()]
                expected_ids = frappe.db.sql(
                    """select a.name from tabActivity a join `tabActivity Type` t on t.name=a.activity_type_tech_id
                    where a.context_tech_id=%s and t.activity_type_enabled_place=1 and t.activity_type_place_required=1
                    and a.activity_place_tech_id is null order by a.name""",
                    (ctx.name,),
                    pluck=True,
                )
                assert [r["name"] for r in rows] == expected_ids
                assert len(rows) == missing and findings.stat().st_mode & 0o777 == 0o600
                assert all(
                    (r["rule"], r["snapshot"], r["context"], r["site"])
                    == ("required_place", snapshot_label, ctx.name, target)
                    for r in rows
                )
                results[ctx.context_code] = {
                    "refused_missing_places": missing,
                    "verified_finding_ids": len(rows),
                }
                continue
            if proc.returncode:
                raise RuntimeError(proc.stderr)
            assert not findings.exists()
            report = json.loads(proc.stdout)
            check = [
                cli,
                "check-publication",
                "--site",
                target,
                "--publication",
                str(out),
                "--run",
                report["run"],
            ]
            verified = json.loads(subprocess.check_output(check, text=True))
            assert verified["verified"] and out.stat().st_mode & 0o777 == 0o600
            expected = snapshot["waves"][ctx.name]["tables"]
            assert report["counts"] == {dt: v["rows"] for dt, v in expected.items()}
            check_published_rows(out, expected)
            again = Path(tmp) / (ctx.context_code + ".sqlite")
            repeated = json.loads(
                subprocess.check_output(
                    [*options, "--publication-out", str(again)], text=True
                )
            )
            assert repeated == report
            with sqlite3.connect(again) as db:
                db.execute("update batches set payload=? where sequence=1", (b"[]",))
            bad = check.copy()
            bad[bad.index(str(out))] = str(again)
            damaged = subprocess.run(bad, check=False, capture_output=True, text=True)
            assert (
                damaged.returncode
                and "batch sequence or contents differ" in damaged.stderr
            )
            results[ctx.context_code] = {
                "run": report["run"],
                "batches": report["batches"],
                "rows": sum(report["counts"].values()),
                "unique_files": report["unique_files"],
                "repeated": True,
                "tampering_rejected": True,
            }
    return results


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-site", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--snapshot-label", default="migration-access-240")
    p.add_argument(
        "--publisher-cli",
        type=Path,
        help="Installed ucm-push executable on the publishing host",
    )
    p.add_argument("--work", required=True, type=Path)
    p.add_argument("--evidence", required=True, type=Path)
    args = p.parse_args()
    out = args.evidence.resolve()
    work = args.work.resolve()
    publisher_cli = (
        args.publisher_cli.resolve(strict=True) if args.publisher_cli else None
    )
    os.chdir(BENCH / "sites")
    frappe.init(site=args.source_site)
    frappe.connect()
    try:
        result = exercise(
            args.source_site, args.target, work, args.snapshot_label, publisher_cli
        )
        with out.open("x") as f:
            json.dump(result, f, indent=2)
        print(json.dumps(result))
    finally:
        frappe.destroy()
