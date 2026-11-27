"""Run the two writer routes serially against matching sealed batches, with live traffic."""

import argparse
import copy
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter
from contextlib import ExitStack
from pathlib import Path
from time import monotonic
from unittest.mock import patch

import frappe
import generator_committed_waves as waves
from benchmark_publication import SITES
from generator_organization_access import exercise as organization_checks
from generator_snapshot import BENCH

from ucm_migration_loader import publication_worker as worker


def write_worker(args, work):
	import benchmark_sql_writer as sql

	runs = json.loads((work / f"{args.route}-ready.json").read_text())
	assert args.run in {r["run"] for r in runs.values()}
	metrics = Counter()
	implementation = sql.write_batch if args.route == "sql" else worker.insert_batch
	reconcile = worker.reconcile_business

	# Each subprocess opens the site afresh. Only its in-process business writer is varied.
	def write(db, context, items, sequence):
		started = monotonic()
		implementation(db, context, items, sequence)
		metrics["write_seconds"] += monotonic() - started
		metrics["write_batches"] += 1
		if sequence % 100 == 0:
			print(
				f"{args.route} batch {sequence} {items[0]['doctype']}",
				file=sys.stderr,
				flush=True,
			)

	def verify(*params):
		started = monotonic()
		result = reconcile(*params)
		metrics["reconciliation_seconds"] += monotonic() - started
		return result

	with ExitStack() as stack:
		stack.enter_context(patch.object(worker, "insert_batch", write))
		stack.enter_context(patch.object(worker, "reconcile_business", verify))
		started = monotonic()
		if args.worker == "activate":
			result = worker.activate(args.run)
		else:
			result = worker.apply(args.run, max_batches=args.limit)
		measured = {
			"run": args.run,
			"method": args.worker,
			"limit": args.limit,
			"total_seconds": monotonic() - started,
			**metrics,
			**sql.TIMES,
			"rows_written_by_experimental_writer": dict(sql.ROWS),
			"result": result,
		}
		with (work / f"{args.route}-phases.jsonl").open("a") as f:
			f.write(json.dumps(measured) + "\n")
		print(json.dumps(result))


def invalid_batch(route, run):
	from benchmark_sql_writer import write_batch

	from ucm_migration_loader.publication_receiver import local_publication

	record = frappe.get_doc("UCM Migration Run", run)
	if record.applied:
		raise ValueError("A timed comparison must start from zero committed incoming batches")
	with sqlite3.connect(local_publication(run).resolve().as_uri() + "?mode=ro", uri=True) as db:
		items = json.loads(db.execute("select payload from batches where sequence=1").fetchone()[0])[:2]
		assert len(items) == 2 and all(i["doctype"] == "Organization" for i in items)
		bad = copy.deepcopy(items)
		bad[1]["row"]["political_power_tech_id"] = "00000000-0000-7000-8000-000000000000"
		writer = worker.insert_batch if route == "orm" else write_batch
		try:
			writer(db, record.context_id, bad, 1)
		except frappe.LinkValidationError as exc:
			finding = type(exc).__name__
		else:
			raise AssertionError("Invalid political power link accepted")
		finally:
			frappe.db.rollback()
	assert not any(frappe.db.exists("Organization", i["row"]["name"]) for i in items)
	assert not frappe.db.count("UCM Migration Batch", {"migration_run": run})
	return finding


def measure(route, work):
	site = SITES[route]
	result_path = work / f"{route}-result.json"
	if result_path.exists():
		raise ValueError("This route already has recorded results")
	runs = json.loads((work / f"{route}-ready.json").read_text())

	rejected = invalid_batch(route, runs["CHA"]["run"])

	def invoke(target, method, run, **kwargs):
		assert target == site
		cmd = [
			sys.executable,
			__file__,
			route,
			"--work",
			str(work),
			"--worker",
			method,
			"--run",
			run,
		]
		if "max_batches" in kwargs:
			cmd.extend(["--limit", str(kwargs["max_batches"])])
		out = subprocess.check_output(cmd, text=True)
		return json.loads(out)

	with patch.object(waves, "invoke", invoke), tempfile.TemporaryDirectory() as tmp:
		measured = waves.exercise(
			site,
			runs["BOL"]["run"],
			runs["CHA"]["run"],
			Path(tmp),
			4,
			True,
			require_error_free=False,
		)
		measured["access_checks"]["organizations"] = organization_checks(Path(tmp))
	measured["invalid_batch_rejected_before_commit"] = rejected
	measured["manifests"] = runs
	with result_path.open("x") as f:
		json.dump(measured, f, indent=2)


if __name__ == "__main__":
	p = argparse.ArgumentParser(description=__doc__)
	p.add_argument("route", choices=SITES)
	p.add_argument("--work", type=Path, required=True)
	p.add_argument("--worker", choices=("apply", "activate"))
	p.add_argument("--limit", type=int, default=1)
	p.add_argument("--run")
	args = p.parse_args()
	work = args.work.resolve()
	os.chdir(BENCH / "sites")
	frappe.init(site=SITES[args.route])
	frappe.connect()
	try:
		if args.worker:
			write_worker(args, work)
		else:
			measure(args.route, work)
	finally:
		frappe.destroy()
