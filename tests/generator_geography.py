"""Load and replay the admin geography used directly by each generator delegation."""

import argparse
import json
import os
from pathlib import Path

import frappe
from generator_collisions import read_site
from generator_references import identifier
from generator_snapshot import BENCH, DIRECT, DYNAMIC, PARENTS, ownership

from ucm_migration_loader.geography import LEVELS, apply_areas, area_plan


def read_plans(source):
	with source.cursor() as cur:
		cur.execute("select name, context_code from tabContext order by context_code")
		contexts = cur.fetchall()
		cur.execute(
			"select parent,fieldname,options from tabDocField where parent in %s and fieldtype='Link' and options in %s",
			(
				(*DIRECT, *DYNAMIC, *PARENTS),
				tuple(f"Admin Level {n}" for n in LEVELS),
			),
		)
		links = cur.fetchall()
		plans = {}
		for ctx, code in contexts:
			direct = {n: set() for n in LEVELS}
			for dt, field, master in links:
				scope, params = ownership(dt)
				cur.execute(
					f"select distinct r.{identifier(field)} from {identifier('tab' + dt)} r {scope}",
					(ctx,) * params,
				)
				direct[int(master[-1])].update(r[0] for r in cur.fetchall() if r[0])
			plans[code] = area_plan(source, direct)
		return plans


def rehearse(source, target):
	if source == target or not target.startswith("ucm-full-wave-") or not target.endswith(".localhost"):
		raise ValueError("Choose an isolated full-wave target")
	with read_site(source) as con:
		plans = read_plans(con)
	frappe.init(site=target)
	frappe.connect()
	try:
		results = {}
		for code, plan in plans.items():
			first = apply_areas(plan)
			frappe.db.commit()
			replay = apply_areas(plan)
			assert all(
				v["created"] == 0 and v["reused"] == len(plan[n])
				for n, v in zip(LEVELS, replay.values(), strict=True)
			)
			results[code] = {"initial": first, "replay": replay}
		return results
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
	result = rehearse(args.source_site, args.target)
	with output.open("x") as f:
		json.dump(result, f, indent=2)
	print(json.dumps(result))
