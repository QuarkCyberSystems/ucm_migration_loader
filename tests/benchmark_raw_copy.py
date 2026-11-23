"""Measure a durable cross-instance SQL copy into private indexed benchmark tables."""

import argparse
import hashlib
import json
import os
from pathlib import Path
from time import monotonic

import frappe
import pymysql
from benchmark_publication import SITES, SOURCE
from generator_collisions import read_site
from generator_snapshot import BENCH, frame, ownership, site_connection
from ucm_migration_loader.frozen import TABLES


def exercise():
    with read_site(SOURCE) as source:
        frappe.init(site=SITES["sql"])
        try:
            with site_connection() as target, target.cursor() as dest:
                with source.cursor() as cur:
                    cur.execute("select name from tabContext where context_code='CHA'")
                    (ctx,) = cur.fetchone()
                results = {}
                # Persistent InnoDB tables use ordinary indexes, redo and commit handling.
                # Never address an application table as the destination for this raw copy.
                for dt in TABLES:
                    dest.execute(f"create table `bench50k {dt}` like `tab{dt}`")
                start = monotonic()
                for dt in TABLES:
                    where, n = ownership(dt)
                    table = f"`bench50k {dt}`"
                    sha = hashlib.sha256()
                    count = 0
                    t = monotonic()
                    with source.cursor(pymysql.cursors.SSCursor) as cur:
                        cur.execute(
                            f"select r.* from `tab{dt}` r {where} order by r.name",
                            (ctx,) * n,
                        )
                        columns = [c[0] for c in cur.description]
                        names = ",".join(f"`{c}`" for c in columns)
                        insert = f"insert into {table} ({names}) values ({','.join(['%s'] * len(columns))})"
                        while rows := cur.fetchmany(1000):
                            dest.executemany(insert, rows)
                            target.commit()
                            for row in rows:
                                sha.update(frame(row))
                            count += len(rows)
                    results[dt] = {
                        "rows": count,
                        "seconds": monotonic() - t,
                        "source_sha256": sha.hexdigest(),
                    }
                    print(dt, count, flush=True)
                copy_time = monotonic() - start
                if results["Person"]["rows"] != 50000:
                    raise ValueError("Expected exactly 50,000 people")
                start = monotonic()
                for dt, stats in results.items():
                    sha = hashlib.sha256()
                    count = 0
                    with target.cursor(pymysql.cursors.SSCursor) as cur:
                        cur.execute(f"select * from `bench50k {dt}` order by name")
                        for row in cur:
                            sha.update(frame(row))
                            count += 1
                    if (
                        sha.hexdigest() != stats["source_sha256"]
                        or count != stats["rows"]
                    ):
                        raise ValueError(f"Stored {dt} differs from source")
                return {
                    "source": SOURCE,
                    "target": SITES["sql"],
                    "context": "CHA",
                    "people": 50000,
                    "copy_and_source_hash_seconds": copy_time,
                    "target_reread_hash_seconds": monotonic() - start,
                    "rows": sum(t["rows"] for t in results.values()),
                    "tables": results,
                    "commit_rows": 1000,
                    "attachment_bytes_transferred": False,
                    "activated": False,
                }
        finally:
            frappe.destroy()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--evidence", type=Path, required=True)
    out = p.parse_args().evidence.resolve()
    assert not out.exists()
    os.chdir(BENCH / "sites")
    result = exercise()
    with out.open("x") as f:
        json.dump(result, f, indent=2)
