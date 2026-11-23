"""Resolve references across the entire sealed wave before committing its first batch."""

import json

import frappe

from .frozen import CHILDREN, LOOKUPS, ORDER
from .publication_worker import resolve_reference, verify_place_address


def verify_links(db):
    # Temporary tables are private to this read-only SQLite connection. Duplicate
    # source keys are errors even across distant batches; shared links are deduplicated.
    db.execute("create temp table incoming(dt,id,primary key(dt,id)) without rowid")
    db.execute("create temp table links(dt,id,primary key(dt,id)) without rowid")
    fields = {}
    for (payload,) in db.execute("select payload from batches order by sequence"):
        for item in json.loads(payload):
            dt = item["doctype"]
            if dt not in ORDER or set(item["children"]) != set(CHILDREN.get(dt, ())):
                raise ValueError("Unexpected document in the sealed inventory")
            rows = [(dt, item["row"])]
            rows.extend(
                (child, r) for child, values in item["children"].items() for r in values
            )
            rows.extend(("File", f["row"]) for f in item.get("files", []))
            for master, row in rows:
                if any(
                    v not in (None, "", "[]")
                    for k, v in row.items()
                    if k.startswith("_")
                ):
                    raise ValueError(
                        f"Populated framework metadata requires mapping for {master}"
                    )
                db.execute("insert into incoming values(?,?)", (master, row["name"]))
                if master not in fields:
                    fields[master] = [
                        (f.fieldname, f.fieldtype, f.options)
                        for f in frappe.get_meta(master).fields
                        if f.fieldtype in ("Link", "Dynamic Link")
                    ]
                links = {("User", row["owner"]), ("User", row["modified_by"])}
                for field, kind, options in fields[master]:
                    if value := row.get(field):
                        target = options if kind == "Link" else row.get(options)
                        if not target:
                            raise ValueError(
                                f"Missing dynamic link type on {master}.{field}"
                            )
                        links.add((target, value))
                db.executemany("insert or ignore into links values(?,?)", links)
    for (key,) in db.execute("select id from refs where dt='Place Address'"):
        verify_place_address(db, {}, key)
    external = db.execute(
        "select dt,id from links except select dt,id from incoming order by dt,id"
    )
    while rows := external.fetchmany(500):
        grouped = {}
        for dt, key in rows:
            grouped.setdefault(dt, []).append(key)
        for dt, keys in grouped.items():
            if dt in LOOKUPS:
                cache = {}
                for key in keys:
                    resolve_reference(db, cache, dt, key)
            elif not frappe.db.exists("DocType", dt) or frappe.db.count(
                dt, {"name": ["in", keys]}
            ) != len(keys):
                raise ValueError(f"Unresolved external {dt} reference")
