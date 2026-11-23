"""Verify full generated person fields, dictionary links, children and private picture bytes."""

import argparse
import json
import os
from collections import Counter
from pathlib import Path

import frappe
from generator_collisions import read_site
from generator_snapshot import BENCH, site_connection
from ucm_migration_loader.persons import insert_person, person_records


def exercise(source, target):
    if (
        source == target
        or not target.startswith("ucm-full-wave-")
        or not target.endswith(".localhost")
    ):
        raise ValueError("Select an isolated full-wave target")
    with read_site(source) as con:
        with con.cursor() as cur:
            cur.execute(
                "select name,context_code from tabContext order by context_code"
            )
            contexts = cur.fetchall()
        private = (BENCH / "sites" / source / "private/files").resolve(strict=True)
        frappe.init(site=target)
        frappe.connect()
        try:
            result = {}
            target_files = Path(frappe.get_site_path("private", "files"))
            before = set(target_files.rglob("*"))
            with site_connection() as target_reader:
                cache = {}
                for ctx, code in contexts:
                    counts = Counter()
                    for record in person_records(con, ctx):
                        counts.update(
                            insert_person(con, target_reader, record, private, cache)
                        )
                    frappe.db.rollback()
                    assert not frappe.db.exists("Person", {"context_tech_id": ctx})
                    assert before == set(target_files.rglob("*"))
                    result[code] = dict(counts)
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
    output = args.evidence.resolve()
    os.chdir(BENCH / "sites")
    result = exercise(args.source_site, args.target)
    with output.open("x") as handle:
        json.dump(result, handle, indent=2)
    print(json.dumps(result))
