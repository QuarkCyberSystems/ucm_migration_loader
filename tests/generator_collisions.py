"""Count already-used generator keys on a target before rehearsing a new delegation."""

import argparse
import json
import os

import frappe
import pymysql
from generator_snapshot import (
    BENCH,
    DIRECT,
    DYNAMIC,
    PARENTS,
    ownership,
    site_connection,
)


def read_site(site):
    frappe.init(site=site)
    try:
        connection = site_connection()
        with connection.cursor() as cur:
            cur.execute("set transaction isolation level repeatable read")
            cur.execute("start transaction with consistent snapshot, read only")
        return connection
    finally:
        frappe.destroy()


def inspect(source, target):
    waves = {}
    with source.cursor() as cursor:
        cursor.execute(
            "select name, context_code from tabContext order by context_code"
        )
        contexts = cursor.fetchall()
    for context, code in contexts:
        counts = {}
        with target.cursor() as destination:
            destination.execute(
                "select count(*) from tabContext where name=%s", context
            )
            context_exists = bool(destination.fetchone()[0])
            for dt in sorted((*DIRECT, *DYNAMIC, *PARENTS)):
                predicate, params = ownership(dt)
                total = collisions = 0
                with source.cursor(pymysql.cursors.SSCursor) as ids:
                    ids.execute(
                        f"select r.name from `tab{dt}` r {predicate} order by r.name",
                        (context,) * params,
                    )
                    while rows := ids.fetchmany(256):
                        keys = tuple(row[0] for row in rows)
                        destination.execute(
                            f"select count(*) from `tab{dt}` where name in %s", (keys,)
                        )
                        collisions += destination.fetchone()[0]
                        total += len(rows)
                counts[dt] = {"source": total, "existing_keys": collisions}
        waves[code] = {
            "context_id_exists": context_exists,
            "tables": counts,
            "existing_keys": sum(t["existing_keys"] for t in counts.values()),
        }
    return waves


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-site", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--evidence", type=argparse.FileType("x"))
    args = parser.parse_args()
    if args.source_site == args.target:
        parser.error("Select distinct source and target sites")
    os.chdir(BENCH / "sites")
    with read_site(args.source_site) as source, read_site(args.target) as target:
        waves = inspect(source, target)
    result = {
        "source_site": args.source_site,
        "target_site": args.target,
        "waves": waves,
    }
    if args.evidence:
        with args.evidence as output:
            json.dump(result, output, indent=2)
            output.write("\n")
    print(json.dumps(result))
