"""Populate and verify generator prerequisites on an isolated full-wave site."""

import argparse
import json
import os
from pathlib import Path

import frappe
from generator_collisions import read_site
from generator_snapshot import BENCH
from ucm_migration_loader.shared_catalogs import apply_catalogs


def exercise(source, target, context=None):
    if (
        source == target
        or not target.startswith("ucm-full-wave-")
        or not target.endswith(".localhost")
    ):
        raise ValueError("Select an isolated full-wave rehearsal site")
    with read_site(source) as con:
        frappe.init(site=target)
        frappe.connect()
        try:
            first = apply_catalogs(con, context=context)
            frappe.db.commit()
            replay = apply_catalogs(con, context=context)
            assert all(not v["created"] for v in replay["catalogs"].values())
            assert first["crosswalk"] == replay["crosswalk"]
            return {"first": first, "replay": replay}
        finally:
            frappe.db.rollback()
            frappe.destroy()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-site", required=True)
    p.add_argument("--target", required=True)
    p.add_argument(
        "--context",
        help="Restrict catalog preparation to the incoming delegation and shared catalogs",
    )
    p.add_argument("--evidence", required=True, type=Path)
    args = p.parse_args()
    output = args.evidence.resolve()
    os.chdir(BENCH / "sites")
    result = exercise(args.source_site, args.target, args.context)
    with output.open("x") as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result))
