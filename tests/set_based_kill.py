"""End the job of a set-based load at chosen points, and check what is left and how the run goes on.

Commands, run from the bench; each prints its result as JSON:

	points   stage a sealed file on the set-based route and carry it through the queue's jobs, each in a process of
	         its own, as a worker's. At every point named in POINTS the job is ended: with `--kill process` its
	         process is killed (SIGKILL), with `--kill connection` its database connection is cut and the job runs
	         into the error. After each one the run is read (nothing half written, nothing locked, what the form
	         shows), the load is asked for again as an operator resumes it, and the next job goes on. The loaded
	         delegation must come out verified with every row compared once; then the activation and the discard
	         are ended the same way. The run ends discarded.
	worker   ask for the load as Desk does and kill the job of the bench's own worker, once for each of
	         `--after` (seconds after the job started), resuming in between. The worker itself is left alone. The
	         run is left ready and hidden.

`--store` and `--label` keep every row of the verified delegation in a local file, to compare with another load
by `set_based_comparison.py diff`. Synthetic data only: the publication is sealed from a site filled by the data
generator. The delegation needs two published DocTypes at least.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import frappe
from set_based_queue import connect, record, stage

BENCH = Path(__file__).resolve().parents[3]
RUN = "UCM Migration Run"
APPLYING, READY, CLEANING = "Publication applying", "Publication ready", "Publication cleaning"
PAUSED = "Publication paused"
# the points, in the order a run meets them: (name, the request whose job is ended there)
POINTS = (
	("write, rows inserted and not committed", "apply"),
	("write, between two transactions", "apply"),
	("derive, between two of UCM's rebuilds", "apply"),
	("verify, after a kept cursor", "apply"),
	("conclude, rules checked and report not kept", "apply"),
	("activate, access released and context opened, not committed", "activate"),
	("discard, between two chunks", "discard"),
)


def expect(condition: bool, message: str) -> None:
	if not condition:
		raise AssertionError(message)


# ---------------------------------------------------------------------------------------------------------------
# the job's process


def _reached(mode: str) -> None:
	"""Tell the parent that the point is reached, with the job's database connection, and wait: for the kill, or
	for the parent to have cut the connection."""
	connection = frappe.db.sql("select connection_id()")[0][0]
	print(json.dumps({"reached": True, "connection": connection}), flush=True)
	if mode == "process":
		time.sleep(3600)
	sys.stdin.readline()


def _at(target, name: str, call: int, mode: str, *, after: bool = False):
	"""Replace a function so that its `call`-th call reaches the point, before it runs or after."""
	original, calls = getattr(target, name), Counter()

	def wrapped(*args, **kwargs):
		calls["n"] += 1
		here = calls["n"] == call
		if here and not after:
			_reached(mode)
		result = original(*args, **kwargs)
		if here and after:
			_reached(mode)
		return result

	return patch.object(target, name, wrapped)


def _hooks(point: str, mode: str, call: int, delete_rows: int) -> list:
	from ucm.ucm.doctype.record_share import record_share

	from ucm_migration_loader import api, constants
	from ucm_migration_loader.ucm_migration_loader import set_based

	return {
		"write, rows inserted and not committed": [_at(set_based, "_receipts", call, mode)],
		"write, between two transactions": [_at(set_based, "_group_rows", call, mode)],
		"derive, between two of UCM's rebuilds": [_at(record_share, "rebuild_activity_facts", 1, mode)],
		"verify, after a kept cursor": [
			patch.object(set_based, "CHECKPOINT", 0),
			_at(set_based, "_compare_batch", call, mode),
		],
		"conclude, rules checked and report not kept": [_at(set_based, "_receipt_chain", 1, mode)],
		"activate, access released and context opened, not committed": [_at(api, "mark_activated", 1, mode)],
		"discard, between two chunks": [
			patch.object(set_based, "DELETE_ROWS", delete_rows),
			_at(set_based, "_counts_moving", call, mode),
		],
	}[point]


def job(site: str, queued: list, point: str | None, mode: str, call: int, delete_rows: int) -> None:
	"""Run one queued job as a worker does: the job, then the commit of what it left open. A job it queues in
	turn is printed for the parent, which runs it next."""
	from ucm_migration_loader import publication_jobs

	connect(site)

	def follow(*next_job):
		print(json.dumps({"queued": next_job}), flush=True)

	hooks = [patch.object(publication_jobs, "enqueue", follow)]
	if point:
		hooks += _hooks(point, mode, call, delete_rows)
	for hook in hooks:
		hook.start()
	try:
		publication_jobs.execute(*queued)
		# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: the end of a queued job, as Frappe's worker commits it
		frappe.db.commit()
	except Exception as exc:
		print(json.dumps({"failed": type(exc).__name__, "message": str(exc)[:200]}), flush=True)
		raise SystemExit(1) from None
	print(json.dumps({"finished": True}), flush=True)


# ---------------------------------------------------------------------------------------------------------------
# the parent


def request(run: str, action: str) -> list:
	"""Ask for an action as Desk and ucm-push do, and take the job it queues instead of the bench's worker."""
	from ucm_migration_loader import publication_jobs

	queued = []
	with patch.object(publication_jobs, "enqueue", lambda *next_job: queued.append(list(next_job))):
		publication_jobs.request(run, action)
	# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: a rehearsal's request, which the job's process must see
	frappe.db.commit()
	expect(len(queued) == 1, f"{action} queued {len(queued)} jobs")
	return queued[0]


def run_job(site: str, queued: list, point: str | None, mode: str, call: int, delete_rows: int) -> dict:
	"""Run one job in a process of its own. Where it reaches the point, end it there."""
	command = [sys.executable, __file__, "job", "--site", site, "--queued", json.dumps(queued)]
	command += ["--kill", mode, "--call", str(call), "--delete-rows", str(delete_rows)]
	if point:
		command += ["--point", point]
	child = subprocess.Popen(
		command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
	)
	out = {"ended": False, "next": None}
	for line in child.stdout:
		try:
			said = json.loads(line)
		except ValueError:
			continue
		if said.get("queued"):
			out["next"] = said["queued"]
		elif said.get("failed"):
			out["failed"], out["message"] = said["failed"], said["message"]
		elif said.get("reached"):
			out["ended"] = True
			if mode == "process":
				os.kill(child.pid, signal.SIGKILL)
			else:
				frappe.db.sql("kill connection %s", said["connection"])
				child.stdin.write("\n")
				child.stdin.flush()
	child.stdin.close()
	out["exit"] = child.wait()
	return out


def drive(site: str, run: str, action: str, point: str | None, mode: str, call: int, delete_rows: int):
	"""Ask for an action and run its jobs, one after another, until one is ended at the point or none follows."""
	queued, jobs = request(run, action), 0
	while queued:
		jobs += 1
		expect(jobs < 5000, "the queue did not come to an end")
		result = run_job(site, queued, point, mode, call, delete_rows)
		if result["ended"]:
			return {**result, "jobs": jobs}
		expect(result["exit"] == 0, f"a job that was not ended failed: {result}")
		queued = result["next"]
	return {"ended": False, "jobs": jobs}


def published(run: str, upto: int) -> dict:
	"""The rows of each table in the publication's batches up to one of them."""
	import sqlite3
	from contextlib import closing

	from ucm_migration_loader.publication_receiver import local_publication

	rows = Counter()
	uri = local_publication(run).resolve().as_uri() + "?mode=ro&immutable=1"
	with closing(sqlite3.connect(uri, uri=True)) as db:
		for (payload,) in db.execute("select payload from batches where sequence <= ?", (upto,)):
			for item in json.loads(payload):
				rows[item["doctype"]] += 1
				for child, children in item["children"].items():
					rows[child] += len(children)
				rows["File"] += len(item.get("files", []))
	return {doctype: count for doctype, count in rows.items() if count}


def unlocked(run: str, seconds: float = 60) -> float:
	"""How long until the run and its context can be locked again: the ended job's transaction is gone."""
	context = frappe.db.get_value(RUN, run, "context_id")
	started = time.monotonic()
	while True:
		frappe.db.rollback()
		try:
			frappe.db.sql("select name from `tabContext` where name = %s for update nowait", context)
			frappe.db.sql("select name from `tabUCM Migration Run` where name = %s for update nowait", run)
			frappe.db.rollback()
			return round(time.monotonic() - started, 2)
		except Exception:
			expect(time.monotonic() - started < seconds, "the ended job still holds the run")
			time.sleep(0.2)


def left(run: str) -> dict:
	"""What an ended job left: the run, the rows of its delegation against the batches it has receipts for, and
	what the form shows."""
	from ucm_migration_loader.ucm_migration_loader import run_overview, set_based

	wait = unlocked(run)
	current = record(run)
	manifest = json.loads(current.manifest_json)
	applied = current.applied or 0
	stored = {dt: n for dt, n in set_based.stored_counts(current.context_id, manifest).items() if n}
	receipts = frappe.db.count("UCM Migration Batch", {"migration_run": run})
	expect(stored == published(run, applied), f"rows differ from the {applied} committed batches: {stored}")
	expect(receipts == applied, f"{receipts} receipts for {applied} committed batches")
	# the form looks a run up in the queue once its worker has been quiet for a while: here, at once
	with patch.object(run_overview, "QUIET", 0):
		shown = run_overview.build(current)
	return {
		"state": current.state,
		"finding": current.finding,
		"job_in_queue": run_overview.working(run),
		"form_says_no_job": shown["stalled"],
		"worker_phase": current.worker_phase,
		"batches_committed": applied,
		"batches_compared": current.reconcile_cursor or 0,
		"rows_stored": sum(stored.values()),
		"seconds_until_unlocked": wait,
		"form": {step["step"]: step["status"] for step in shown["steps"]},
	}


def _ended(result: dict, point: str, mode: str) -> None:
	expect(result["ended"], f"the load never reached: {point}")
	if mode == "process":
		expect(result["exit"] == -signal.SIGKILL, f"the job was not killed: {result}")
	else:
		expect(result["exit"] != 0 and result.get("failed"), f"the job outlived its connection: {result}")


def load(site: str, run: str, mode: str, calls: dict, delete_rows: int) -> tuple[dict, dict]:
	"""End the load at each of its points and resume it, to the verified delegation."""
	out, before = {}, left(run)
	for point, action in POINTS:
		if action != "apply":
			continue
		result = drive(site, run, "apply", point, mode, calls.get(point, 1), delete_rows)
		_ended(result, point, mode)
		after = left(run)
		if mode == "process":
			# a killed job says nothing: the run keeps its state, and the form finds no job in the queue
			expect(after["state"] == APPLYING and not after["finding"], f"{point}: {after}")
			expect(after["form_says_no_job"], f"{point}: the form does not say that no job is on the run")
		else:
			# a job that lost its connection takes a new one and pauses the run with what failed
			expect(after["state"] == PAUSED, f"{point}: the run is {after['state']}")
			expect((after["finding"] or "").endswith("OperationalError"), f"{point}: {after['finding']}")
		expect(after["batches_committed"] >= before["batches_committed"], f"{point}: receipts went back")
		out[point] = {"jobs_before": result["jobs"] - 1, "job_error": result.get("failed"), **after}
		before = after
	finish = drive(site, run, "apply", None, mode, 1, delete_rows)
	current = record(run)
	report = json.loads(current.reconciliation_json or "{}")
	expect(current.state == READY, f"the resumed load ended as {current.state}: {current.finding}")
	expect(not report.get("findings"), f"findings after the resumed load: {report.get('findings')}")
	counts = json.loads(current.manifest_json)["counts"]
	business = {dt: n for dt, n in counts.items() if n and dt not in ("ToDo", "Record Share")}
	expect(report["rows_compared"] == business, "a row was compared twice or not at all")
	expect({dt: n for dt, n in report["stored_rows"].items() if n} == business, "stored rows differ")
	ready = {
		"state": current.state,
		"jobs": finish["jobs"],
		"rows_compared": sum(report["rows_compared"].values()),
		"rules_checked": report["rules_checked"],
		"sampled_document_checks": report["sampled_document_checks"],
		"deferred_access_rows": report["deferred_access_rows"],
	}
	return out, ready


def activation(site: str, run: str, mode: str) -> dict:
	"""End the activation before its commit: the delegation stays hidden and nothing of its access is left. Then
	the same activation, in this process and rolled back, goes through."""
	from ucm_migration_loader.ucm_migration_loader import set_based

	point = "activate, access released and context opened, not committed"
	result = drive(site, run, "activate", point, mode, 1, 0)
	_ended(result, point, mode)
	after = left(run)
	current = record(run)
	context = frappe.db.get_value(
		"Context", current.context_id, ["context_status", "context_discoverability"], as_dict=True
	)
	access = {
		"Record Share": frappe.db.count("Record Share", {"context_tech_id": current.context_id}),
		"ToDo": set_based.stored_counts(current.context_id, {"counts": {"ToDo": 0}})["ToDo"],
		"UCM Migration Held Share": frappe.db.count("UCM Migration Held Share", {"migration_run": run}),
	}
	expect(after["state"] == READY and not current.approved_by, f"the run is {after['state']}")
	if mode == "connection":
		expect((after["finding"] or "").endswith("OperationalError"), f"activation: {after['finding']}")
	expect(
		(context.context_status, context.context_discoverability) == ("Locked", "Hidden"),
		f"the context is {context}",
	)
	expect(not any(access.values()), f"access rows were left: {access}")
	again = set_based.activate(run, commit=False)
	expect(record(run).state == READY, "the rolled-back activation changed the run")
	return {
		point: {"job_error": result.get("failed"), **after, "context": context, "access_rows_left": access},
		"activation_afterwards_rolled_back": {
			"released_access_rows": again["released_access_rows"],
			"shares_held_at_activation": again["shares_held_at_activation"],
			"seconds": again["seconds"],
		},
	}


def discard(site: str, run: str, mode: str, call: int, delete_rows: int) -> dict:
	"""End the discard between two of its chunks: the run stays cleaning, and the next request removes the rest."""
	from ucm_migration_loader.ucm_migration_loader import set_based

	point = "discard, between two chunks"
	loaded = sum(json.loads(record(run).reconciliation_json)["stored_rows"].values())
	result = drive(site, run, "discard", point, mode, call, delete_rows)
	_ended(result, point, mode)
	unlocked(run)
	current = record(run)
	manifest = json.loads(current.manifest_json)
	rows = sum(set_based.stored_counts(current.context_id, manifest).values())
	expect(current.state == CLEANING, f"the run is {current.state}")
	expect(0 < rows < loaded, f"{rows} of {loaded} rows are left")
	drive(site, run, "discard", None, mode, 1, delete_rows)
	current = record(run)
	gone = {dt: n for dt, n in set_based.stored_counts(current.context_id, manifest).items() if n}
	expect(current.state == "Discarded" and not gone, f"the run is {current.state}, rows left: {gone}")
	return {
		point: {"job_error": result.get("failed"), "state": CLEANING, "rows_left": rows, "of": loaded},
		"discard_afterwards": {"state": current.state, "rows_left": gone},
	}


def points(args) -> dict:
	from set_based_comparison import snapshot

	stage(args.publication, args.run)
	calls = {
		"write, rows inserted and not committed": args.write_call,
		"write, between two transactions": 2,
		"verify, after a kept cursor": args.verify_call,
	}
	started = time.monotonic()
	ended, ready = load(args.site, args.run, args.kill, calls, args.delete_rows)
	out = {"kill": args.kill, **ended, "ready": {**ready, "seconds": round(time.monotonic() - started, 1)}}
	if args.store:
		out["rows_kept_for_comparison"] = sum(
			snapshot(record(args.run).context_id, args.store, args.label).values()
		)
	out.update(activation(args.site, args.run, args.kill))
	out.update(discard(args.site, args.run, args.kill, args.discard_call, args.delete_rows))
	return out


# ---------------------------------------------------------------------------------------------------------------
# the bench's own worker


def _started(run: str) -> bool:
	"""Whether a job of this run is at work in the migration queue (not only waiting in it)."""
	from frappe.utils.background_jobs import get_queue

	from ucm_migration_loader import api, constants

	queue = get_queue(constants.QUEUE)
	jobs = [queue.fetch_job(job_id) for job_id in queue.started_job_registry.get_job_ids()]
	return any(one and (one.kwargs.get("kwargs") or {}).get("run") == run for one in jobs)


def _working(run: str, seconds: float) -> tuple[int, int] | None:
	"""The process in which the bench's worker runs a job of this run, and that worker's own process. A worker
	runs each job in a child of its own, with its command line. Only such a child is ever returned, and only
	while it is the one job process on the bench and a job of this run is at work: the worker itself keeps the
	queue, and another run's job is not this test's to end."""
	deadline = time.monotonic() + seconds
	while time.monotonic() < deadline:
		listed = subprocess.run(
			["ps", "-eo", "pid=,ppid=,args="], capture_output=True, text=True, check=False
		)
		workers = {}
		for line in listed.stdout.splitlines():
			pid, parent, command = line.split(None, 2)
			if command.rstrip().endswith("frappe.utils.bench_helper frappe worker"):
				workers[int(pid)] = int(parent)
		jobs = [(pid, parent) for pid, parent in workers.items() if parent in workers]
		if len(jobs) == 1 and _started(run):
			return jobs[0]
		time.sleep(0.05)
	return None


def _job_registries(run: str) -> dict:
	"""Where the queue keeps the jobs of this run."""
	from frappe.utils.background_jobs import get_queue

	from ucm_migration_loader import api, constants

	queue = get_queue(constants.QUEUE)
	out = {}
	for name, registry in (
		("queued", queue),
		("started", queue.started_job_registry),
		("failed", queue.failed_job_registry),
	):
		jobs = [queue.fetch_job(job_id) for job_id in registry.get_job_ids()]
		out[name] = sum(1 for one in jobs if one and (one.kwargs.get("kwargs") or {}).get("run") == run)
	return out


def worker(args) -> dict:
	"""Kill the bench's own worker's job of a load, and resume it, once for each delay."""
	from set_based_queue import follow

	from ucm_migration_loader import api, constants

	if args.publication:
		stage(args.publication, args.run)
	out, kills = {"kills": []}, list(args.after)
	while True:
		api.apply(args.run)
		# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: a rehearsal's request, which the worker must see
		frappe.db.commit()
		if not kills:
			break
		delay = kills.pop(0)
		found = _working(args.run, 60)
		expect(found is not None, "the worker did not take the job")
		horse, parent = found
		time.sleep(delay)
		try:
			os.kill(horse, signal.SIGKILL)
		except ProcessLookupError:
			out["kills"].append({"after": delay, "job_had_finished": True, "state": record(args.run).state})
			if record(args.run).state == READY:
				return {**out, "state": READY, "note": "the load was ready before every kill was made"}
			continue
		time.sleep(3)  # the worker notices the dead job and files it
		after = left(args.run)
		expect(after["job_in_queue"] is False, "the queue still holds the killed job")
		expect(after["form_says_no_job"], "the form does not say that no job is on the run")
		alive = subprocess.run(["kill", "-0", str(parent)], check=False).returncode == 0
		expect(alive, "the worker went down with its job")
		expect(after["state"] == APPLYING and not after["finding"], f"after the kill: {after}")
		out["kills"].append(
			{"after": delay, **after, "worker_alive": alive, "queue": _job_registries(args.run)}
		)
	timeline, seconds = follow(args.run, args.timeout, (APPLYING,))
	current = record(args.run)
	report = json.loads(current.reconciliation_json or "{}")
	expect(current.state == READY, f"the resumed load ended as {current.state}: {current.finding}")
	counts = json.loads(current.manifest_json)["counts"]
	business = {dt: n for dt, n in counts.items() if n and dt not in ("ToDo", "Record Share")}
	expect(report["rows_compared"] == business, "a row was compared twice or not at all")
	return {
		**out,
		"state": current.state,
		"findings": report.get("findings"),
		"rows_compared": sum(report["rows_compared"].values()),
		"seconds_after_last_resume": seconds,
		"timeline": timeline,
	}


def main() -> None:
	parser = argparse.ArgumentParser(
		description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
	)
	parser.add_argument("command", choices=["points", "worker", "job"])
	parser.add_argument("--site", required=True)
	parser.add_argument("--run")
	parser.add_argument("--publication", type=Path, help="Stage this sealed file first")
	parser.add_argument("--kill", choices=["process", "connection"], default="process")
	parser.add_argument("--write-call", type=int, default=1, help="The transaction of the write to end")
	parser.add_argument("--verify-call", type=int, default=2, help="The batch of the verification to end at")
	parser.add_argument("--discard-call", type=int, default=3, help="The chunk of the discard to end at")
	parser.add_argument("--delete-rows", type=int, default=400, help="Rows per chunk of the discard")
	parser.add_argument("--after", type=float, nargs="*", default=[0.3, 1.0], help="worker: seconds per kill")
	parser.add_argument("--timeout", type=float, default=3600, help="worker: seconds to follow the worker")
	parser.add_argument("--store", type=Path, help="Keep the verified delegation's rows in this local file")
	parser.add_argument("--label", default="after the kills")
	parser.add_argument("--evidence", type=Path, help="Write the result here as well")
	parser.add_argument("--queued", help="job: the queued job, as JSON")
	parser.add_argument("--point", help="job: the point to end at")
	parser.add_argument("--call", type=int, default=1, help="job: the call that reaches the point")
	args = parser.parse_args()
	if args.command == "job":
		job(args.site, json.loads(args.queued), args.point, args.kill, args.call, args.delete_rows)
		return
	if not args.run:
		parser.error("Supply --run")
	args.publication = args.publication.resolve() if args.publication else None
	evidence = args.evidence.resolve() if args.evidence else None
	args.store = args.store.resolve() if args.store else None
	connect(args.site)
	try:
		result = points(args) if args.command == "points" else worker(args)
	finally:
		frappe.destroy()
	text = json.dumps(result, indent=1, default=str)
	print(text)
	if evidence:
		evidence.write_text(text + "\n")


if __name__ == "__main__":
	main()
