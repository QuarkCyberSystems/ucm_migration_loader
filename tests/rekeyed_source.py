"""Read a generator source under other record keys, so that its delegations can be loaded on a site that the same
generator filled.

The generator computes its keys from an entity code and a sequence number: two sites it filled hold the same keys.
A delegation of one cannot be published to the other as it is: its persons, cases and activities are already
there, under another context. This module puts a view over the source connection that moves every key a
delegation owns to a key space of its own (the second digit of the entity code), and leaves the shared reference
data (countries, areas, places, activity types) under the keys both sites know. Nothing is written to the source.

Three commands, run from the bench; each prints its result as JSON:

	plan      read the source and the target, and keep the plan: the entity codes the delegations own, the few
	          owned rows of catalogs that also hold shared rows, the sensitivity tags both sites have under
	          different keys, and the codes that must be unique on the target
	prepare   create the delegations' contexts on the target, locked and hidden, with their own catalogs
	          (rolled back unless --commit)
	freeze    seal one delegation of the source, under its new keys, into a publication for the target

Synthetic data only: the source is a site filled by the data generator.
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import pymysql

BENCH = Path(__file__).resolve().parents[3]
KEY = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-7)([0-9a-f])([0-9a-f]{2})(-8000-[0-9a-f]{12})")
GENERATED = "^[0-9a-f]{8}-[0-9a-f]{4}-70[0-9a-f]{2}-8000-[0-9a-f]{12}$"
TASK = re.compile(r"bx[tw]\d{7}")


def connect(site: str):
	"""A read connection to a site's database, as its own user."""
	conf = json.loads((BENCH / "sites" / site / "site_config.json").read_text())
	common = json.loads((BENCH / "sites" / "common_site_config.json").read_text())
	socket = conf.get("db_socket") or common.get("db_socket")
	where = (
		{"unix_socket": socket}
		if socket
		else {"host": conf.get("db_host") or common.get("db_host") or "127.0.0.1"}
	)
	return pymysql.connect(
		user=conf.get("db_user") or conf["db_name"],
		password=conf["db_password"],
		database=conf["db_name"],
		**where,
	)


class Rekeyed:
	"""A source connection whose results carry the new keys and whose parameters are taken back to the old."""

	def __init__(self, connection, plan: dict):
		self.connection = connection
		self.digit = plan["digit"]
		self.codes = frozenset(plan["codes"])
		self.forward = dict(plan["keys"])
		self.forward.update(plan["values"])
		self.back = {new: old for old, new in self.forward.items()}
		self.listed = dict(plan["values"])

	def cursor(self, *args, **kwargs):
		return _Cursor(self.connection.cursor(*args, **kwargs), self)

	def close(self) -> None:
		self.connection.close()

	def _key(self, match, source: str, target: str) -> str:
		if match.group(2) != source or match.group(3) not in self.codes:
			return match.group(0)
		return match.group(1) + target + match.group(3) + match.group(4)

	def out(self, value):
		"""A value read from the source, as the publication carries it."""
		if type(value) is not str or len(value) < 5:
			return value
		if value in self.forward:
			return self.forward[value]
		if "," in value and not self.listed.keys().isdisjoint(value.split(",")):
			# the tag codes a record carries: UCM keeps them in order
			return ",".join(sorted(self.listed.get(code, code) for code in value.split(",")))
		if "-70" in value:
			return KEY.sub(lambda match: self._key(match, "0", self.digit), value)
		if len(value) == 10 and value.startswith("bxt") and TASK.fullmatch(value):
			return "bxw" + value[3:]
		return value

	def into(self, value):
		"""A parameter of a query, as the source stores it."""
		if isinstance(value, (list, tuple)):
			return type(value)(self.into(item) for item in value)
		if isinstance(value, dict):
			return {key: self.into(item) for key, item in value.items()}
		if type(value) is not str or len(value) < 5:
			return value
		if value in self.back:
			return self.back[value]
		if f"-7{self.digit}" in value:
			return KEY.sub(lambda match: self._key(match, self.digit, "0"), value)
		if len(value) == 10 and value.startswith("bxw") and TASK.fullmatch(value):
			return "bxt" + value[3:]
		return value


class _Cursor:
	def __init__(self, cursor, view: Rekeyed):
		self.cursor, self.view = cursor, view

	def __enter__(self):
		self.cursor.__enter__()
		return self

	def __exit__(self, *exc):
		return self.cursor.__exit__(*exc)

	def __getattr__(self, name):
		return getattr(self.cursor, name)

	def execute(self, query, args=None):
		return self.cursor.execute(query, self.view.into(args))

	def _row(self, row):
		if row is None:
			return None
		out = self.view.out
		if isinstance(row, dict):
			return {key: out(value) for key, value in row.items()}
		return tuple(out(value) for value in row)

	def fetchone(self):
		return self._row(self.cursor.fetchone())

	def fetchall(self):
		return [self._row(row) for row in self.cursor.fetchall()]

	def fetchmany(self, size=None):
		return [self._row(row) for row in self.cursor.fetchmany(size)]

	def __iter__(self):
		for row in self.cursor:
			yield self._row(row)


def make_plan(source_site: str, target_site: str, prefix: str, digit: str) -> dict:
	"""Read both sites and say what moves: see the module's description."""
	from ucm_migration_loader.frozen import CHILDREN

	owned, shared, mixed_tables = set(), set(), {}
	children = {f"tab{child}" for group in CHILDREN.values() for child in group}
	with connect(source_site) as source, source.cursor() as cur:
		cur.execute(
			"select table_name from information_schema.columns"
			" where table_schema = database() and column_name = 'context_tech_id'"
		)
		scoped = {table for (table,) in cur.fetchall()}
		cur.execute(
			"select table_name from information_schema.tables"
			" where table_schema = database() and table_name like 'tab%%' and table_rows > 0"
		)
		tables = sorted(table for (table,) in cur.fetchall() if table in scoped or table in children)
		per_table = {}
		for table in tables:
			mine = "context_tech_id is not null" if table in scoped else "1"
			# nosemgrep: frappe-semgrep-rules.rules.security.frappe-sql-format-injection -- reviewed: table names read from the source's own schema; no values
			cur.execute(
				f"select substr(cast(name as char), 16, 3), {mine}, count(*) from `{table}`"
				f" where cast(name as char) regexp '{GENERATED}' group by 1, 2"
			)
			for code, is_owned, _count in cur.fetchall():
				(owned if is_owned else shared).add(code[1:])
				per_table.setdefault(table, set()).add((code[1:], bool(is_owned)))
		keys = {}
		for table, found in per_table.items():
			both = {code for code, mine in found if mine} & {code for code, mine in found if not mine}
			if both:
				mixed_tables[table] = sorted(both)
				# nosemgrep: frappe-semgrep-rules.rules.security.frappe-sql-format-injection -- reviewed: table names read from the source's own schema; no values
				cur.execute(f"select cast(name as char) from `{table}` where context_tech_id is not null")
				for (name,) in cur.fetchall():
					keys[name] = name[:15] + digit + name[16:]
		cur.execute("select name, context_code, context_name from tabContext order by context_code")
		contexts = [
			{"name": name, "source_code": code, "code": prefix + code, "label": label}
			for name, code, label in cur.fetchall()
		]
		cur.execute("select cast(name as char), tag_code, context_tech_id from `tabSensitivity Tag`")
		tags = cur.fetchall()
	values = {}
	with connect(target_site) as target, target.cursor() as cur:
		for name, code, context in tags:
			if context:
				values[code] = prefix + code  # unique on the target, where the site's own delegation has it
				continue
			cur.execute(
				"select cast(name as char) from `tabSensitivity Tag` where tag_code = %s and context_tech_id is null",
				code,
			)
			match = cur.fetchall()
			if len(match) != 1:
				raise ValueError(f"The target has no single shared sensitivity tag {code}")
			keys[name] = match[0][0]
	return {
		"source": source_site,
		"target": target_site,
		"digit": digit,
		"codes": sorted(owned - shared),
		"catalogs_with_shared_rows": mixed_tables,
		"keys": keys,
		"values": values,
		"contexts": contexts,
	}


def prepare(plan: dict, commit: bool) -> dict:
	"""The contexts of the source on the target, locked and hidden, each with the catalogs it owns."""
	import frappe

	from ucm_migration_loader.scope import CATALOGS
	from ucm_migration_loader.shared_catalogs import apply_catalogs

	os.chdir(BENCH / "sites")
	frappe.init(site=plan["target"])
	frappe.connect()
	# nosemgrep: frappe-semgrep-rules.rules.security.frappe-setuser -- reviewed: a rehearsal run from the bench by its operator; the loader's commands require a System Manager
	frappe.set_user("Administrator")
	out = {}
	try:
		with connect(plan["source"]) as raw:
			source = Rekeyed(raw, plan)
			for context in plan["contexts"]:
				if not frappe.db.exists("Context", context["name"]):
					with source.cursor() as cur:
						cur.execute(
							"select context_country_tech_id from tabContext where name = %s", context["name"]
						)
						(country,) = cur.fetchone()
					frappe.get_doc(
						{
							"doctype": "Context",
							"context_code": context["code"],
							"context_name": f"Migration rehearsal {context['label']}",
							"context_country_tech_id": country,
							"context_status": "Locked",
							"context_discoverability": "Hidden",
							"context_description": "Synthetic generator delegation, re-keyed for a migration rehearsal",
						}
					).insert(set_name=context["name"])
			for context in plan["contexts"]:
				catalogs = tuple(catalog for catalog in CATALOGS if catalog != "Context")
				result = apply_catalogs(source, catalogs=catalogs, context=context["name"])
				out[context["code"]] = {
					catalog: counts for catalog, counts in result["catalogs"].items() if counts["created"]
				}
		if commit:
			# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: the prepared contexts and catalogs, kept on the operator's word (--commit)
			frappe.db.commit()
		else:
			frappe.db.rollback()
		return {"committed": commit, "created": out}
	finally:
		frappe.db.rollback()
		frappe.destroy()


def freeze(plan: dict, code: str, snapshot: str, output: Path, size: int) -> dict:
	from ucm_migration_loader.frozen import freeze as seal

	(context,) = [context["name"] for context in plan["contexts"] if context["code"] == code]
	started = time.monotonic()
	with connect(plan["source"]) as raw:
		with raw.cursor() as cur:
			cur.execute("set transaction isolation level repeatable read")
			cur.execute("start transaction with consistent snapshot, read only")
		published = seal(
			Rekeyed(raw, plan),
			output,
			(BENCH / "sites" / plan["source"] / "private/files").resolve(),
			plan["target"],
			snapshot,
			context,
			size,
			4 * 1024 * 1024,
		)
	return {
		"run": published["run"],
		"context": context,
		"batches": published["batches"],
		"rows": sum(published["counts"].values()),
		"counts": published["counts"],
		"file_bytes": output.stat().st_size,
		"seconds": round(time.monotonic() - started, 2),
	}


def main() -> None:
	parser = argparse.ArgumentParser(
		description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
	)
	parser.add_argument("step", choices=["plan", "prepare", "freeze"])
	parser.add_argument(
		"--plan", required=True, type=Path, help="The plan's file: written by plan, read by the others"
	)
	parser.add_argument("--source-site")
	parser.add_argument("--site", help="The target site")
	parser.add_argument("--prefix", help="Put before the codes that must be unique on the target")
	parser.add_argument("--digit", default="2", help="The key space the delegations move to: 1 to f")
	parser.add_argument("--commit", action="store_true", help="Keep what prepare created")
	parser.add_argument("--context", help="The delegation to seal, by its code on the target")
	parser.add_argument("--snapshot-id")
	parser.add_argument("--publication", type=Path)
	parser.add_argument("--batch-size", type=int, default=1000)
	parser.add_argument("--evidence", type=Path, help="Write the result here as well")
	args = parser.parse_args()
	plan_file = args.plan.resolve()
	evidence = args.evidence and args.evidence.resolve()
	if args.step == "plan":
		if not re.fullmatch("[1-9a-f]", args.digit) or not args.prefix:
			parser.error("plan needs --prefix and a --digit from 1 to f")
		result = make_plan(args.source_site, args.site, args.prefix, args.digit)
		plan_file.write_text(json.dumps(result, indent=1) + "\n")
	elif args.step == "prepare":
		result = prepare(json.loads(plan_file.read_text()), args.commit)
	else:
		publication = args.publication.resolve()
		result = freeze(
			json.loads(plan_file.read_text()), args.context, args.snapshot_id, publication, args.batch_size
		)
	text = json.dumps(result, indent=1, default=str)
	print(text)
	if evidence:
		evidence.write_text(text + "\n")


if __name__ == "__main__":
	sys.exit(main())
