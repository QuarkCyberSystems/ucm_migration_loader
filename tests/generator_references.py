"""Compare external Link prerequisites in each published delegation with the proposed target."""

import argparse
import json
import os
from collections import Counter
from pathlib import Path

import pymysql
from generator_collisions import read_site
from generator_snapshot import BENCH, DIRECT, DYNAMIC, PARENTS, ownership

from ucm_migration_loader.dictionary_identity import TERM_FIELDS as TERM
from ucm_migration_loader.dictionary_identity import resolve_term


def identifier(value):
	return "`" + value.replace("`", "``") + "`"


def catalog(conn, dt, keys):
	cols = ("name", *TERM) if dt == "Concept Value" else ("name",)
	with conn.cursor() as cur:
		cur.execute(
			f"select {','.join(map(identifier, cols))} from {identifier('tab' + dt)} where name in %s",
			(keys,),
		)
		return {r[0]: r[1:] for r in cur.fetchall()}


def assess(source, target):
	types = tuple(sorted((*DIRECT, *PARENTS, *DYNAMIC)))
	with source.cursor() as cur:
		cur.execute(
			"select parent, fieldname, options from tabDocField where parent in %s and fieldtype='Link' order by parent,fieldname",
			(types,),
		)
		links = [(dt, field, master) for dt, field, master in cur.fetchall() if master not in types]
		cur.execute("select name,context_code from tabContext order by context_code")
		contexts = cur.fetchall()
	waves = {}
	for ctx, code in contexts:
		fields = {}
		for dt, field, master in links:
			scope, params = ownership(dt)
			column = "r." + identifier(field)
			# Dynamic ownership predicates include OR, so filter out NULL in the outer query.
			sql = f"select distinct ref from (select {column} as ref from {identifier('tab' + dt)} r {scope}) owned where ref is not null"
			counts = Counter()
			with source.cursor(pymysql.cursors.SSCursor) as ids:
				ids.execute(sql, (ctx,) * params)
				# Check target existence in bounded groups while streaming source keys.
				while rows := ids.fetchmany(256):
					keys = tuple(r[0] for r in rows if r[0] != "")
					if not keys:
						continue
					matches = catalog(target, master, keys)
					counts["references"] += len(keys)
					counts["missing_on_target"] += len(keys) - len(matches)
			if counts and master == "Concept Value":
				columns = ",".join("v." + identifier(f) for f in TERM)
				with source.cursor(pymysql.cursors.SSCursor) as cur:
					cur.execute(
						f"select k.ref,v.name,{columns} from ({sql}) k left join `tabConcept Value` v on v.name=k.ref",
						(ctx,) * params,
					)
					while rows := cur.fetchmany(256):
						found = catalog(target, master, tuple(r[0] for r in rows))
						for row in rows:
							if row[1] is not None and row[0] not in found:
								_, status = resolve_term(target, row[0], row[2:])
								counts["term_" + status] += 1
						counts["missing_source_term"] += sum(r[1] is None for r in rows)
						counts["changed_term"] += sum(
							r[1] is not None and r[0] in found and found[r[0]] != r[2:] for r in rows
						)
			if counts:
				fields[f"{dt}.{field}"] = {"link_to": master, **counts}
		waves[code] = fields
	return waves


if __name__ == "__main__":
	p = argparse.ArgumentParser(description=__doc__)
	p.add_argument("--source-site", required=True)
	p.add_argument("--target", required=True)
	p.add_argument("--evidence", required=True, type=Path)
	args = p.parse_args()
	output = args.evidence.resolve()
	os.chdir(BENCH / "sites")
	with read_site(args.source_site) as source, read_site(args.target) as target:
		waves = assess(source, target)
	issues = {
		code: sum(
			any(row.get(k, 0) for k in ("missing_on_target", "missing_source_term", "changed_term"))
			for row in fields.values()
		)
		for code, fields in waves.items()
	}
	with output.open("x") as f:
		json.dump(
			{
				"source": args.source_site,
				"target": args.target,
				"fields_with_findings": issues,
				"waves": waves,
			},
			f,
			indent=2,
		)
	print(json.dumps(issues))
