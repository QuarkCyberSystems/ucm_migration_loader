"""Provision the frozen country references on the isolated full-wave rehearsal site."""

import argparse
import json
import os

import frappe
from generator_collisions import read_site
from generator_snapshot import BENCH
from ucm_migration_loader.countries import apply_countries, read_countries


def exercise(source, target):
    if (
        not target.startswith("ucm-full-wave-")
        or not target.endswith(".localhost")
        or source == target
    ):
        raise ValueError("Choose the isolated full-wave target")
    with read_site(source) as con:
        rows = read_countries(con)
    frappe.init(site=target)
    frappe.connect()
    try:
        first = apply_countries(rows)
        frappe.db.commit()
        again = apply_countries(rows)
        assert again == {"created": 0, "reused": len(rows)}
        altered = dict(rows[0], country_name="Differing synthetic publication")
        try:
            apply_countries([altered])
        except ValueError as exc:
            if "fields differ" not in str(exc):
                raise
        else:
            raise AssertionError("A conflicting country was accepted")
        frappe.db.rollback()
        assert apply_countries([rows[0]])["reused"] == 1
        return {"first": first, "replay": again, "divergence_rejected": True}
    finally:
        frappe.db.rollback()
        frappe.destroy()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-site", required=True)
    p.add_argument("--target", required=True)
    args = p.parse_args()
    os.chdir(BENCH / "sites")
    print(json.dumps(exercise(args.source_site, args.target)))
