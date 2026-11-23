"""Check the fixture volume generator output and measure what the current migration profile covers."""

import argparse
import getpass
import json
import os
import subprocess
import tempfile
from importlib import import_module
from pathlib import Path

import frappe
import pymysql
from generator_snapshot import site_snapshot
from transport import BENCH, publication_reader
from ucm_migration_loader.contract import FIELDS, OTHER_NAMES, PROFILE, TABLES, plan
from ucm_migration_loader.push import publication, publisher_schema
from ucm_migration_loader.scope import assess


def inspect(site, label, target):
    from frappe.model import no_value_fields
    volume = import_module(f"{os.environ['UCM_FIXTURE_APP']}.demo.volume")

    root = Path(frappe.get_site_path("private", "benchmark"))
    if Path(label).name != label:
        raise ValueError("Choose a generator result label")
    source_report = json.loads((root / f"{label}-volume.json").read_text())
    check = volume.check()
    if not check["ok"]:
        raise ValueError(
            f"Generator consistency checks failed: {[k for k, v in check['zero'].items() if v]}"
        )
    if not frappe.conf.db_socket or not frappe.db.sql("select @@skip_networking")[0][0]:
        raise ValueError("Use the dedicated socket-only publishing instance")
    tables = {**volume.TABLES, **volume.ACCESS_TABLES}
    counts = {}
    for key, table in tables.items():
        # The generator reports delegation sensitive rows separately from shared geography profiles.
        filters = (
            {"record_doctype": ["in", ["Person", "Activity", "Activity Detail"]]}
            if key == "sensitive"
            else {}
        )
        count = frappe.db.count(table.removeprefix("tab"), filters)
        expected = source_report["counts"][key]
        if key == "share":
            expected += source_report["counts"].get("share_user", 0)
        if count != expected:
            raise ValueError(f"Source changed after generation for {table}")
        counts[table.removeprefix("tab")] = count
    extra = {}
    for dt in TABLES:
        carried = set(FIELDS.get(dt, OTHER_NAMES)) | {
            "context_tech_id",
            "person_all_names",
        }
        fields = [
            df.fieldname
            for df in frappe.get_meta(dt).fields
            if df.fieldtype not in no_value_fields
            and not df.is_virtual
            and df.fieldname not in carried
        ]
        if fields:
            # Identifiers come from installed DocType metadata. Zero numbers and false checks are populated values.
            expressions = ",".join(
                f"sum(`{f}` is not null and length(cast(`{f}` as char)) > 0)"
                for f in fields
            )
            values = frappe.db.sql(f"select {expressions} from `tab{dt}`")[0]
            extra[dt] = {
                field: int(n) for field, n in zip(fields, values, strict=True) if n
            }
    cfg = frappe.conf
    waves = []
    with pymysql.connect(
        unix_socket=cfg.db_socket,
        user=cfg.db_user or cfg.db_name,
        password=cfg.db_password,
        database=cfg.db_name,
    ) as con:
        with con.cursor() as cur:
            cur.execute("start transaction with consistent snapshot, read only")
        schema = publisher_schema(con)
        for context in frappe.get_all(
            "Context", fields=["name", "context_code"], order_by="context_code"
        ):
            manifest, batches = plan(
                target, label, context.name, publication(con, context.name), size=25
            )
            manifest["schema"] = schema
            result = {
                dt: sum(doc["doctype"] == dt for batch in batches for doc in batch)
                for dt in FIELDS
            }
            waves.append(
                {
                    "context": context.name,
                    "context_code": context.context_code,
                    "batches": len(batches),
                    "scope": assess(con, context.name),
                    "parent_counts": result,
                }
            )
    with (
        pymysql.connect(
            unix_socket=cfg.db_socket, user=getpass.getuser(), autocommit=True
        ) as admin,
        admin.cursor() as cur,
        tempfile.TemporaryDirectory(prefix="ucm-generator-plan-") as folder,
        publication_reader(cur, cfg.db_name, cfg.db_socket, Path(folder)) as config,
    ):
        for wave in waves:
            options = [
                "--context",
                wave["context"],
                "--site",
                target,
                "--snapshot",
                label,
                "--source-config",
                str(config),
            ]
            cli = str(BENCH / "env/bin/ucm-push")
            assessed = subprocess.run(
                [cli, "assess", *options], capture_output=True, text=True, check=True
            )
            assert (
                json.loads(assessed.stdout)["outside_profile_rows"]
                == wave["scope"]["outside_profile_rows"]
            )
            ordered = subprocess.check_output(
                [cli, "activity-order", *options, "--batch-size", "25"], text=True
            )
            wave["activity_order"] = json.loads(ordered)
            assert wave["activity_order"]["activities"] == wave["scope"][
                "outside_profile_rows"
            ].get("Activity", 0)
            if not wave["scope"]["eligible_for_push"]:
                rejected = subprocess.run(
                    [cli, "push", *options], capture_output=True, text=True, check=False
                )
                assert (
                    rejected.returncode == 1
                    and not json.loads(rejected.stdout)["eligible_for_push"]
                )
                wave["push_refused_before_network"] = True
    if any(sum(w["parent_counts"][dt] for w in waves) != counts[dt] for dt in FIELDS):
        raise ValueError("Pilot source plans omit a generated parent row")
    snapshot = site_snapshot([wave["context"] for wave in waves])
    expected_counts = {**counts, "ToDo": source_report["tasks"]}
    for dt, expected in expected_counts.items():
        total = sum(w["tables"][dt]["rows"] for w in snapshot["waves"].values())
        if total != expected:
            raise ValueError(
                f"Delegation partition for {dt} totals {total}, expected {expected}"
            )
    pictures = sum(w["tables"]["File"]["rows"] for w in snapshot["waves"].values())
    if pictures != frappe.db.count("File", {"attached_to_doctype": "Person"}):
        raise ValueError("Person attachment missing from delegation partition")
    return {
        "delegation_snapshot": snapshot,
        "source_site": site,
        "generator": f"{volume.__name__}.load",
        "label": label,
        "consistency_ok": True,
        "zero_checks": check["zero"],
        "profile": PROFILE,
        "planned_for": target,
        "waves": waves,
        "generated_rows": counts,
        "generated_tables_outside_profile": {
            dt: n for dt, n in counts.items() if dt not in TABLES and n
        },
        "populated_fields_outside_profile": extra,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-site", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    output = args.evidence.resolve()
    os.chdir(BENCH / "sites")
    frappe.init(site=args.source_site)
    frappe.connect()
    try:
        result = inspect(args.source_site, args.label, args.target)
        with output.open("x", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2)
            handle.write("\n")
        print(
            json.dumps(
                {
                    "source_site": args.source_site,
                    "waves": result["waves"],
                    "outside_profile": result["generated_tables_outside_profile"],
                }
            )
        )
    finally:
        frappe.destroy()
