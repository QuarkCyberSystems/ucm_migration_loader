"""Compare a delegation loaded by the set-based SQL route with the same delegation loaded through the documents.

Each step is a command, run from the bench with the loader's source on PYTHONPATH, and writes its result as JSON:

	snapshot    every row of the delegation, per table, into a local SQLite file under a label
	counts      the exact row count of every table of the site
	set-aside   copy a document-route load to backup tables, remove it with the set-based delete, retire its run
	freeze      seal the delegation again from its source site, under a new snapshot identity
	load        stage, apply, derive and verify by the set-based route, with the time of each step
	reconcile   the document route's own reconciliation over the rows the set-based route wrote (rolled back)
	activate    time the set-based activation and roll it back: the delegation stays hidden
	diff        compare two snapshots row by row and field by field
	restore     put the backup rows back in place of a set-based load

Synthetic data only: the sources are sites filled by the data generator.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from contextlib import closing
from pathlib import Path

import frappe

BENCH = Path(__file__).resolve().parents[3]
BACKUP = "zz_setbased_backup_"
IGNORED_TABLES = ("tabUCM Migration", BACKUP, "tabRQ Job", "tabError Log", "__")


def connect(site: str) -> None:
	os.chdir(BENCH / "sites")
	frappe.init(site=site)
	frappe.connect()
	# nosemgrep: frappe-semgrep-rules.rules.security.frappe-setuser -- reviewed: a rehearsal run from the bench by its operator; the loader's commands require a System Manager
	frappe.set_user("Administrator")


def tables() -> list[str]:
	from ucm_migration_loader.frozen import TABLES

	return list(TABLES)


def snapshot(context: str, store: Path, label: str) -> dict:
	"""Every row of the delegation, as JSON, keyed by table and name."""
	from ucm_migration_loader.ucm_migration_loader.set_based import _scope

	out = {}
	with closing(sqlite3.connect(store)) as db:
		db.execute(
			"create table if not exists rows (label, doctype, name, row, primary key (label, doctype, name))"
		)
		db.execute("delete from rows where label = ?", (label,))
		for doctype in tables():
			source, values = _scope(doctype, context)
			# nosemgrep: frappe-semgrep-rules.rules.security.frappe-sql-format-injection -- reviewed: the scope is built in code from the publication profile; the context is a parameter
			rows = frappe.db.sql(f"select r.* {source}", values, as_dict=True)
			db.executemany(
				"insert into rows values (?, ?, ?, ?)",
				(
					(label, doctype, str(row["name"]), json.dumps(row, sort_keys=True, default=str))
					for row in rows
				),
			)
			out[doctype] = len(rows)
		db.commit()
	return out


def table_counts() -> dict:
	"""Exact rows of every table: what a load leaves outside the tables it is known to write."""
	out = {}
	for (table,) in frappe.db.sql(
		"select table_name from information_schema.tables where table_schema = database() and table_type = 'BASE TABLE'"
	):
		if table.startswith(IGNORED_TABLES):
			continue
		# nosemgrep: frappe-semgrep-rules.rules.security.frappe-sql-format-injection -- reviewed: the table names are read from this database's own catalogue
		out[table] = frappe.db.sql(f"select count(*) from `{table}`")[0][0]
	return out


def diff(store: Path, first: str, second: str) -> dict:
	"""Rows only in one snapshot, and for rows in both the fields whose values differ, counted per table."""
	out = {}
	with closing(sqlite3.connect(store)) as db:
		for (doctype,) in db.execute(
			"select distinct doctype from rows where label in (?, ?)", (first, second)
		).fetchall():
			only_first = db.execute(
				"""select count(*) from rows a where a.label = ? and a.doctype = ? and not exists
				(select 1 from rows b where b.label = ? and b.doctype = a.doctype and b.name = a.name)""",
				(first, doctype, second),
			).fetchone()[0]
			only_second = db.execute(
				"""select count(*) from rows a where a.label = ? and a.doctype = ? and not exists
				(select 1 from rows b where b.label = ? and b.doctype = a.doctype and b.name = a.name)""",
				(second, doctype, first),
			).fetchone()[0]
			fields, same = Counter(), 0
			for left, right in db.execute(
				"""select a.row, b.row from rows a join rows b on b.doctype = a.doctype and b.name = a.name
				where a.label = ? and b.label = ? and a.doctype = ?""",
				(first, second, doctype),
			):
				if left == right:
					same += 1
					continue
				left, right = json.loads(left), json.loads(right)
				fields.update(key for key in left.keys() | right.keys() if left.get(key) != right.get(key))
			out[doctype] = {
				"identical_rows": same,
				f"only_in_{first}": only_first,
				f"only_in_{second}": only_second,
				"fields_differing": dict(fields),
			}
	return out


def examples(store: Path, first: str, second: str, doctype: str, field: str, limit: int = 3) -> list:
	"""Pairs of values of one differing field, for the operator of a synthetic rehearsal to read."""
	found = []
	with closing(sqlite3.connect(store)) as db:
		for left, right in db.execute(
			"""select a.row, b.row from rows a join rows b on b.doctype = a.doctype and b.name = a.name
			where a.label = ? and b.label = ? and a.doctype = ? and a.row <> b.row""",
			(first, second, doctype),
		):
			left, right = json.loads(left), json.loads(right)
			if left.get(field) != right.get(field):
				found.append([left.get(field), right.get(field)])
				if len(found) == limit:
					break
	return found


def set_aside(run: str) -> dict:
	"""Copy the rows of a document-route load to backup tables, remove them by the set-based delete, and retire
	the run that loaded them (its receipts and its sealed file stay)."""
	from ucm_migration_loader.ucm_migration_loader.set_based import HIDDEN, _scope, delete_delegation

	record = frappe.get_doc("UCM Migration Run", run)
	if record.state not in HIDDEN:
		raise ValueError("Only a hidden run can be set aside")
	context, copied = record.context_id, {}
	for number, doctype in enumerate(tables()):
		source, values = _scope(doctype, context)
		backup = f"{BACKUP}{number:02d}"
		frappe.db.sql_ddl(f"drop table if exists `{backup}`")
		# nosemgrep: frappe-semgrep-rules.rules.security.frappe-sql-format-injection -- reviewed: the scope is built in code from the publication profile; the context is a parameter
		frappe.db.sql(f"create table `{backup}` as select r.* {source}", values)
		copied[doctype] = frappe.db.sql(f"select count(*) from `{backup}`")[0][0]
	started = time.monotonic()
	removed = delete_delegation(context, keep_files=True)
	seconds = round(time.monotonic() - started, 2)
	if removed != {doctype: count for doctype, count in copied.items() if count}:
		raise ValueError(f"Removed rows differ from the backup: {removed} / {copied}")
	frappe.db.set_value(
		"UCM Migration Run",
		run,
		{
			"state": "Discarded",
			"finding": "Set aside for the set-based comparison; rows kept in backup tables",
		},
	)
	frappe.db.commit()
	return {
		"context": context,
		"backed_up": copied,
		"delete_seconds": seconds,
		"rows_deleted": sum(removed.values()),
	}


def restore(context: str) -> dict:
	"""Put the backup rows back, in place of whatever the delegation holds."""
	from ucm_migration_loader.ucm_migration_loader.set_based import delete_delegation

	delete_delegation(context, keep_files=True)
	restored = {}
	for number, doctype in enumerate(tables()):
		backup = f"{BACKUP}{number:02d}"
		frappe.db.sql(f"insert into `tab{doctype}` select * from `{backup}`")
		restored[doctype] = frappe.db.sql("select row_count()")[0][0]
		frappe.db.commit()
	return restored


def freeze(source_site: str, context: str, site: str, snapshot_id: str, output: Path, size: int) -> dict:
	import pymysql

	from ucm_migration_loader.frozen import freeze as seal

	conf = json.loads((BENCH / "sites" / source_site / "site_config.json").read_text())
	common = json.loads((BENCH / "sites" / "common_site_config.json").read_text())
	socket = conf.get("db_socket") or common.get("db_socket")
	where = (
		{"unix_socket": socket}
		if socket
		else {"host": conf.get("db_host") or common.get("db_host") or "127.0.0.1"}
	)
	started = time.monotonic()
	with pymysql.connect(
		user=conf.get("db_user") or conf["db_name"],
		password=conf["db_password"],
		database=conf["db_name"],
		**where,
	) as source:
		with source.cursor() as cur:
			cur.execute("set transaction isolation level repeatable read")
			cur.execute("start transaction with consistent snapshot, read only")
		published = seal(
			source,
			output,
			(BENCH / "sites" / source_site / "private/files").resolve(),
			site,
			snapshot_id,
			context,
			size,
			4 * 1024 * 1024,
		)
	return {
		"run": published["run"],
		"batches": published["batches"],
		"rows": sum(published["counts"].values()),
		"counts": published["counts"],
		"file_bytes": output.stat().st_size,
		"seconds": round(time.monotonic() - started, 2),
	}


def load(path: Path | None, run: str, sample_every: int) -> dict:
	from ucm_migration_loader.publication_receiver import stage
	from ucm_migration_loader.ucm_migration_loader import set_based

	out = {}
	if path:
		started = time.monotonic()
		out["stage"] = {**stage(str(path), run), "seconds": round(time.monotonic() - started, 2)}
		frappe.db.commit()
	out["apply"] = set_based.apply(run)
	out["derive"] = set_based.derive(run)
	out["verify"] = set_based.verify(run, sample_every)
	return out


def reconcile(run: str) -> dict:
	"""The document route's own reconciliation of every stored record, over rows the set-based route wrote."""
	from ucm_migration_loader.application import lock_run
	from ucm_migration_loader.publication_worker import reconcile_business
	from ucm_migration_loader.ucm_migration_loader.set_based import _checked_manifest, _publication

	started = time.monotonic()
	with _publication(run) as db:
		manifest = _checked_manifest(db, run)
		ctx, record = lock_run(run)
		try:
			reconcile_business(db, ctx, record, manifest)
			report = json.loads(record.reconciliation_json)
		finally:
			frappe.db.rollback()
	return {
		"passed": True,
		"checked_business_rows": report["checked_business_rows"],
		"seconds": round(time.monotonic() - started, 2),
	}


def main() -> None:
	parser = argparse.ArgumentParser(
		description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
	)
	parser.add_argument(
		"step",
		choices=[
			"snapshot",
			"counts",
			"set-aside",
			"freeze",
			"load",
			"reconcile",
			"activate",
			"diff",
			"examples",
			"restore",
		],
	)
	parser.add_argument("--site", required=True)
	parser.add_argument("--context")
	parser.add_argument("--run")
	parser.add_argument("--label")
	parser.add_argument("--against")
	parser.add_argument("--doctype")
	parser.add_argument("--field")
	parser.add_argument("--source-site")
	parser.add_argument("--snapshot-id")
	parser.add_argument("--publication", type=Path)
	parser.add_argument("--batch-size", type=int, default=1000)
	parser.add_argument("--sample-every", type=int, default=50)
	parser.add_argument("--store", type=Path, help="Local SQLite file for the snapshots")
	parser.add_argument("--evidence", type=Path, help="Write the step's result here as well")
	args = parser.parse_args()
	if args.step in ("diff", "examples"):
		result = (
			diff(args.store, args.label, args.against)
			if args.step == "diff"
			else examples(args.store, args.label, args.against, args.doctype, args.field)
		)
	elif args.step == "freeze":
		result = freeze(
			args.source_site, args.context, args.site, args.snapshot_id, args.publication, args.batch_size
		)
	else:
		connect(args.site)
		try:
			if args.step == "snapshot":
				result = snapshot(args.context, args.store, args.label)
			elif args.step == "counts":
				result = table_counts()
			elif args.step == "set-aside":
				result = set_aside(args.run)
			elif args.step == "load":
				result = load(args.publication, args.run, args.sample_every)
			elif args.step == "reconcile":
				result = reconcile(args.run)
			elif args.step == "activate":
				from ucm_migration_loader.ucm_migration_loader import set_based

				result = set_based.activate(args.run, commit=False)
			else:
				result = restore(args.context)
		finally:
			frappe.destroy()
	text = json.dumps(result, indent=1, default=str)
	if args.evidence:
		args.evidence.write_text(text + "\n")
	print(text)


if __name__ == "__main__":
	sys.exit(main())
