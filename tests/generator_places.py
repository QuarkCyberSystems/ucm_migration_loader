"""Import and replay all generated casework place dependencies on the isolated target."""

import argparse
import json
import os
from collections import Counter
from pathlib import Path

import frappe
from generator_collisions import read_site
from generator_snapshot import BENCH
from ucm_migration_loader.places import apply_place, place_keys
from ucm_migration_loader.shared_catalogs import apply_catalogs


def exercise(source, target):
    if (
        source == target
        or not target.startswith("ucm-full-wave-")
        or not target.endswith(".localhost")
    ):
        raise ValueError("Select an isolated full-wave target")
    with read_site(source) as conn:
        with conn.cursor() as c:
            c.execute("select name,context_code from tabContext order by context_code")
            contexts = c.fetchall()
        frappe.init(site=target)
        frappe.connect()
        try:
            mappings = {
                (r["doctype"], r["published"]): r["target"]
                for r in apply_catalogs(conn)["crosswalk"]
            }
            result = {}
            for ctx, code in contexts:
                keys = place_keys(conn, ctx)
                result[code] = {}
                for phase in ("initial", "replay"):
                    totals = Counter()
                    for key in keys:
                        totals.update(apply_place(conn, key, mappings))
                    frappe.db.commit()
                    result[code][phase] = dict(totals)
                assert not result[code]["replay"].get("created")
            return result
        finally:
            frappe.db.rollback()
            frappe.destroy()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-site", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--evidence", required=True, type=Path)
    args = p.parse_args()
    out = args.evidence.resolve()
    os.chdir(BENCH / "sites")
    result = exercise(args.source_site, args.target)
    with out.open("x") as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result))
