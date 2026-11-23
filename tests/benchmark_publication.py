"""Freeze the same 50k wave for two isolated targets and prepare their shared catalogs."""

import argparse
import json
import os
import shutil
import sqlite3
from pathlib import Path
from time import monotonic

import frappe
import generator_catalogs
import generator_countries
import generator_geography
import generator_places
from generator_collisions import read_site
from generator_committed_waves import fixture
from generator_snapshot import BENCH
from ucm_migration_loader.contract import digest, encoded
from ucm_migration_loader.frozen import freeze, verify_publication
from ucm_migration_loader.publication_receiver import stage

SOURCE = "ucm-generator-wave50k.localhost"
SITES = {route: f"ucm-full-wave-bench50k-{route}.localhost" for route in ("orm", "sql")}


def publications(work):
    root = BENCH / "sites" / SOURCE / "private/files"
    with read_site(SOURCE) as conn, conn.cursor() as cur:
        for code, people in (("BOL", 250), ("CHA", 50000)):
            cur.execute("select name from tabContext where context_code=%s", code)
            (ctx,) = cur.fetchone()
            cur.execute("select count(*) from tabPerson where context_tech_id=%s", ctx)
            assert cur.fetchone() == (people,)
            src = work / f"{code}-orm.sqlite"
            if not src.exists():
                freeze(
                    conn,
                    src,
                    root,
                    SITES["orm"],
                    "wave50k",
                    ctx,
                    25,
                    262144,
                    work / f"{code}-findings.jsonl",
                )
            with sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True) as db:
                original = json.loads(
                    db.execute("select manifest from descriptor").fetchone()[0]
                )
            assert (
                original["site"] == SITES["orm"]
                and original["counts"]["Person"] == people
            )
            dest = work / f"{code}-sql.sqlite"
            manifest = dict(original, site=SITES["sql"])
            if not dest.exists():
                temp = work / f"{code}-sql.partial"
                temp.touch(mode=0o600, exist_ok=False)
                shutil.copyfile(src, temp)
                with sqlite3.connect(temp) as db:
                    db.execute("update descriptor set manifest=?", (encoded(manifest),))
                temp.rename(dest)
            verify_publication(dest, digest(manifest))


def prepare(work, route, catalogs_only=False, first_only=False):
    site = SITES[route]
    marker = work / f"{route}-catalogs.json"
    if not marker.exists():
        generator_countries.exercise(SOURCE, site)
        generator_geography.rehearse(SOURCE, site)
        generator_catalogs.exercise(SOURCE, site)
        generator_places.exercise(SOURCE, site)
        marker.write_text(json.dumps({"site": site, "source": SOURCE}))
    if catalogs_only:
        return
    frappe.init(site=site)
    frappe.connect()
    try:
        receipts = {}
        for code in (("BOL",) if first_only else ("BOL", "CHA")):
            path = work / f"{code}-{route}.sqlite"
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
                m = json.loads(
                    db.execute("select manifest from descriptor").fetchone()[0]
                )
            run = digest(m)
            started = monotonic()
            stage(str(path), run)
            frappe.db.commit()
            fixture(site, run)
            receipts[code] = {
                "run": run,
                "counts": m["counts"],
                "chain": m["batch_chain_sha256"],
                "stage_and_fixture_seconds": monotonic() - started,
            }
        (work / f"{route}-ready.json").write_text(json.dumps(receipts, indent=2))
    finally:
        frappe.destroy()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("phase", choices=["freeze", *SITES])
    p.add_argument("--work", required=True, type=Path)
    p.add_argument("--catalogs-only", action="store_true")
    p.add_argument("--first-only", action="store_true")
    args = p.parse_args()
    work = args.work.resolve()
    work.mkdir(exist_ok=True, mode=0o700)
    os.chdir(BENCH / "sites")
    if args.phase == "freeze":
        publications(work)
    else:
        prepare(work, args.phase, args.catalogs_only, args.first_only)
