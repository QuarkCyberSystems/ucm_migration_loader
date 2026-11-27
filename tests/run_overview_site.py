"""Bring a rehearsal site's runs into the situations the run form has to show, and hand a run's metrics over.

Commands, run from the bench; each prints its result as JSON:

	findings   stage a sealed file, write and derive it on the set-based route, change a few of its hidden rows
	           directly in the database, and verify: the run must pause with exactly those findings, and its
	           overview and metrics must say so. The run stays paused and hidden.
	refused    stage a sealed file the site must refuse (its record IDs are already there): the staging fails,
	           and a refused run is kept that shows what differs, without a file and without a row.
	handover   run `ucm-push quality` as the migration side does, over TLS and with a publishing account made
	           for this test: the metrics go to a CSV file and to a table of a stand-in data quality database,
	           and the run records the hand-over. Another account is refused.
	ready      check what a run that came across whole shows, on either route: every step done with its time,
	           the same rows on both sides, metrics without a failure. The run is only read.
	activated  check what an activated run shows: who opened the delegation, when and in how long, the
	           assignments and shares released, and metrics that count them. The run is only read.

Synthetic data only: the publications are sealed from a site filled by the data generator.
"""

import argparse
import csv
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path

import frappe

BENCH = Path(__file__).resolve().parents[3]
RUN = "UCM Migration Run"
CHANGED = " (changed)"
NOT_AN_OPTION = "Not an option"


def connect(site: str) -> None:
	os.chdir(BENCH / "sites")
	frappe.init(site=site)
	frappe.connect()
	# nosemgrep: frappe-semgrep-rules.rules.security.frappe-setuser -- reviewed: a rehearsal run from the bench by its operator; the loader's commands require a System Manager
	frappe.set_user("Administrator")


def record(run: str):
	frappe.db.rollback()  # a new read of the run, past this connection's snapshot
	return frappe.get_doc(RUN, run)


def overview(run: str) -> dict:
	from ucm_migration_loader.ucm_migration_loader import run_overview

	return run_overview.build(record(run))


def metrics(run: str) -> dict:
	from ucm_migration_loader import api

	frappe.db.rollback()
	return api.quality(run)


def expect(condition: bool, message: str) -> None:
	if not condition:
		raise AssertionError(message)


# ---------------------------------------------------------------------------------------------------------------
# findings


def _items(run: str, sample_every: int):
	"""The publication's records that the verification will not pass through the documents: (doctype, item). A
	sampled record that differs stops the verification at once, which is another path than its findings."""
	from ucm_migration_loader.publication_receiver import local_publication

	uri = local_publication(run).resolve().as_uri() + "?mode=ro"
	seen = 0
	with closing(sqlite3.connect(uri, uri=True)) as db:
		for (payload,) in db.execute("select payload from batches order by sequence"):
			for item in json.loads(payload):
				seen += 1
				if seen % sample_every:
					yield item["doctype"], item


def _select(doctype: str) -> str:
	"""A Select field of a DocType whose first option is a value, which a save sets where the field is empty."""
	for df in frappe.get_meta(doctype).fields:
		options = (df.options or "").split("\n")
		if df.fieldtype == "Select" and len(options) > 1 and options[0] and not df.reqd:
			return df.fieldname
	raise ValueError(f"{doctype} has no such Select field")


def _text(item: dict) -> str:
	"""A published text field of a Person that holds a value."""
	meta = frappe.get_meta("Person")
	for field, value in item["row"].items():
		df = meta.get_field(field)
		if df and df.fieldtype == "Data" and isinstance(value, str) and len(value) > 2 and not df.options:
			return field
	raise ValueError("The Person has no text field to change")


def tamper(run: str, sample_every: int) -> dict:
	"""Change a hidden delegation's rows behind the loader's back: one text, three option values, two defaults
	emptied, one child row removed. Raw SQL on purpose: the point is rows no controller has seen."""
	text, child, cases, persons = None, None, [], []
	for doctype, item in _items(run, sample_every):
		if doctype == "Person" and text is None:
			text = item
		elif doctype == "Person" and child is None and item["children"].get("Person Address"):
			child = item
		elif doctype == "Person" and len(persons) < 2:
			persons.append(item["row"]["name"])
		elif doctype == "Case" and len(cases) < 3:
			cases.append(item["row"]["name"])
		if text and child and len(persons) == 2 and len(cases) == 3:
			break
	field, option, default = _text(text), _select("Case"), _select("Person")
	address = child["children"]["Person Address"][0]["name"]
	# nosemgrep: frappe-semgrep-rules.rules.security.frappe-sql-format-injection -- reviewed: the fields are read from the DocTypes of this site; every value is a parameter
	frappe.db.sql(
		f"update `tabPerson` set `{field}` = concat(`{field}`, %s) where name = %s",
		(CHANGED, text["row"]["name"]),
	)
	# nosemgrep: frappe-semgrep-rules.rules.security.frappe-sql-format-injection -- reviewed: as above
	frappe.db.sql(f"update `tabCase` set `{option}` = %s where name in %s", (NOT_AN_OPTION, tuple(cases)))
	# nosemgrep: frappe-semgrep-rules.rules.security.frappe-sql-format-injection -- reviewed: as above
	frappe.db.sql(f"update `tabPerson` set `{default}` = null where name in %s", (tuple(persons),))
	frappe.db.sql("delete from `tabPerson Address` where name = %s", (address,))
	# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: the verification reads in transactions of its own
	frappe.db.commit()
	return {"text": field, "option": option, "default": default}


def findings(run: str, path: Path, sample_every: int) -> dict:
	from ucm_migration_loader import write_routes
	from ucm_migration_loader.publication_receiver import stage
	from ucm_migration_loader.ucm_migration_loader import set_based

	if not frappe.db.exists(RUN, run):
		stage(str(path), run)
		frappe.db.commit()
		write_routes.select(run, write_routes.SET_BASED)
		frappe.db.commit()
	staged = overview(run)
	expect(staged["steps"][0]["seconds"] is not None, "staging left no time on the run")
	# a run this command left written or derived continues from there
	if {step["step"]: step["status"] for step in staged["steps"]}["write"] != "done":
		set_based.apply(run)
	if "derived" not in json.loads(record(run).reconcile_counts or "{}"):
		set_based.derive(run)
	fields = tamper(run, sample_every)
	report = set_based.verify(run, sample_every)
	expect(report["state"] == "Publication paused", f"the run is {report['state']} after the changes")
	shown = overview(run)
	found = {(row["doctype"], row["check"], row["detail"]): row for row in shown["findings"]}
	expected = {
		("Person", "field differs from the publication", fields["text"]): (1, "CRITICAL"),
		("Person", "field differs from the publication", fields["default"]): (2, "CRITICAL"),
		("Person", "rule broken", f"{fields['default']}: empty where a save sets its default"): (2, "MEDIUM"),
		("Case", "field differs from the publication", fields["option"]): (3, "CRITICAL"),
		("Case", "rule broken", f"{fields['option']}: not an option"): (3, "HIGH"),
		("Person Address", "rows missing in UCM", ""): (1, "CRITICAL"),
		("Person Address", "published rows not found by their key", ""): (1, "CRITICAL"),
	}
	got = {key: (row["found"], row["severity"]) for key, row in found.items()}
	expect(got == expected, f"findings differ: {got}")
	expect(
		all(row["batch"] for key, row in found.items() if key[1].startswith("field")), "a place is missing"
	)
	steps = {step["step"]: step["status"] for step in shown["steps"]}
	expect(steps["verify"] == "failed" and steps["activate"] == "waiting", f"steps: {steps}")
	document = metrics(run)
	failed = [row for row in document["metrics"] if row["passed"] is False]
	by_severity = {
		row["rule"]: row["value"]
		for row in document["metrics"]
		if row["metric"] == "Errors count by severity"
	}
	expect(
		by_severity["HIGH"] == 1 and by_severity["MEDIUM"] == 1 and by_severity["CRITICAL"] >= 5,
		str(by_severity),
	)
	current = record(run)
	return {
		"run": run,
		"display_id": current.display_id,
		"state": current.state,
		"finding": current.finding,
		"changed_fields": fields,
		"findings": [{k: v for k, v in row.items() if v not in (None, "")} for row in shown["findings"]],
		"metrics": len(document["metrics"]),
		"metrics_failed": len(failed),
		"errors_by_severity": by_severity,
		"list_columns": [current.delegation, current.write_route, current.duration],
	}


# ---------------------------------------------------------------------------------------------------------------
# refused


def refused(run: str, path: Path) -> dict:
	from ucm_migration_loader.publication_receiver import local_publication, stage

	try:
		stage(str(path), run)
	except ValueError as exc:
		expect("preflight failed" in str(exc), f"staging failed for another reason: {exc}")
	else:
		raise AssertionError("the site staged a file it had to refuse")
	current = record(run)
	shown = overview(run)
	expect(current.state == "Rejected", f"the run is {current.state}")
	expect(not local_publication(run).exists(), "a refused file was kept")
	expect(not frappe.db.count("UCM Migration Batch", {"migration_run": run}), "a refused run has receipts")
	step = shown["steps"]
	expect(len(step) == 1 and step[0]["status"] == "failed", f"steps: {step}")
	keys = step[0]["facts"]["existing_keys"]
	expect(sum(keys.values()) > 0, "the refusal does not say which record IDs exist")
	return {
		"run": run,
		"display_id": current.display_id,
		"state": current.state,
		"finding": current.finding,
		"seconds": step[0]["seconds"],
		"tables_with_existing_ids": len(keys),
		"existing_ids": sum(keys.values()),
		"tables_that_differ": step[0]["facts"]["schema_differences"],
	}


# ---------------------------------------------------------------------------------------------------------------
# handover


def _table_rows(config: Path, table: str, run: str) -> int:
	import pymysql

	with pymysql.connect(**json.loads(config.read_text())) as db, db.cursor() as cursor:
		# nosemgrep: frappe-semgrep-rules.rules.security.frappe-sql-format-injection -- reviewed: the table is this test's own argument, checked by the writer before; the run is a parameter
		cursor.execute(f"select count(*), sum(passed = 0) from `{table}` where run = %s", (run,))
		return cursor.fetchone()


def _drop(config: Path, table: str) -> None:
	import pymysql

	with pymysql.connect(**json.loads(config.read_text())) as db, db.cursor() as cursor:
		# nosemgrep: frappe-semgrep-rules.rules.security.frappe-sql-format-injection -- reviewed: the table this test created
		cursor.execute(f"drop table if exists `{table}`")
		db.commit()


def handover(site: str, run: str, config: Path, table: str, keep: bool) -> dict:
	from transport import publisher_identity, tls_site

	before = record(run)
	previous = before.publishing_user
	expect(table.endswith("_rehearsal"), "name the rehearsal's table ..._rehearsal: it is dropped at the end")
	with (
		tempfile.TemporaryDirectory(prefix="ucm-quality-") as folder,
		tls_site(site, Path(folder)) as (url, cert),
		publisher_identity() as (publisher, token),
		publisher_identity() as (_stranger, other_token),
	):
		work = Path(folder)
		# the account that sent a run's file is kept on the run when the file is received (receive)
		frappe.db.set_value(RUN, run, "publishing_user", publisher, update_modified=False)
		frappe.db.commit()
		env = {
			**os.environ,
			"NO_PROXY": "localhost",
			"no_proxy": "localhost",
			"REQUESTS_CA_BUNDLE": str(cert),
		}

		def cli(*options, credential=token, code=0):
			result = subprocess.run(
				[
					str(BENCH / "env/bin/ucm-push"),
					"quality",
					"--site",
					site,
					"--url",
					url,
					"--run",
					run,
					*options,
				],
				env={**env, "UCM_MIGRATION_TOKEN": credential},
				check=False,
				capture_output=True,
				text=True,
				timeout=300,
			)
			if result.returncode != code:
				raise RuntimeError(f"ucm-push quality ended with {result.returncode}: {result.stderr[-400:]}")
			return json.loads(result.stdout) if code == 0 else result.stderr

		try:
			document = metrics(run)
			out = work / "metrics.csv"
			database = ("--quality-config", str(config), "--quality-table", table)
			first = cli("--quality-out", str(out), *database, "--create-quality-table")
			rows = list(csv.DictReader(out.open()))
			stored, failing = _table_rows(config, table, run)
			expect(first["metrics"] == len(rows) == stored == len(document["metrics"]), "row counts differ")
			expect(
				first["failed"] == int(failing or 0), "failed metrics differ between the file and the table"
			)
			expect({row["run"] for row in rows} == {run}, "the file holds another run")
			as_json = work / "metrics.json"
			cli("--quality-out", str(as_json))
			expect(json.loads(as_json.read_text())["sha256"] == document["sha256"], "the JSON file differs")
			again = cli(*database)
			expect(_table_rows(config, table, run)[0] == stored, "a second hand-over doubled the rows")
			step = next(step for step in overview(run)["steps"] if step["step"] == "hand over")
			expect(step["status"] == "done" and step["facts"]["by"] == publisher, f"hand-over step: {step}")
			expect(step["facts"]["destination"] == "table " + table, f"destination: {step['facts']}")
			expect(record(run).modified == before.modified, "the hand-over changed the run's own time")
			expect(metrics(run)["sha256"] == document["sha256"], "the metrics changed with the hand-over")
			denied = cli("--quality-out", str(work / "no.csv"), credential=other_token, code=1)
			expect(
				"HTTP 403" in denied and not (work / "no.csv").exists(), "another account read the metrics"
			)
		finally:
			frappe.db.rollback()
			frappe.db.set_value(RUN, run, "publishing_user", previous, update_modified=False)
			frappe.db.commit()
			if not keep:
				_drop(config, table)
	return {
		"run": run,
		"state": first["state"],
		"metrics": first["metrics"],
		"failed": first["failed"],
		"table": table,
		"table_rows": stored,
		"second_hand_over": again["destination"],
		"another_account": "refused with HTTP 403",
	}


# ---------------------------------------------------------------------------------------------------------------
# ready, activated


def _step(shown: dict, name: str) -> dict:
	return next((step for step in shown["steps"] if step["step"] == name), {})


def _loaded(run: str) -> tuple:
	"""The checks a ready and an activated run share: the steps of its route, each done and timed, and the
	business rows equal on both sides."""
	current, shown, document = record(run), overview(run), metrics(run)
	route = shown["facts"]["route"]
	work = (
		["stage", "write", "derive", "verify"] if route == "Set-based SQL" else ["stage", "write", "verify"]
	)
	names = [step["step"] for step in shown["steps"]]
	expect(names[: len(work)] == work, f"steps of a {route} run: {names}")
	for name in work:
		step = _step(shown, name)
		expect(step["status"] == "done", f"{name} is {step['status']}")
		# a run staged before the stage was timed has no time for it
		expect(name == "stage" or step["seconds"] is not None, f"{name} has no time")
	expect(not shown["findings"], f"findings: {shown['findings']}")
	business = [row for row in shown["records"] if not row["deferred"] and row["published"]]
	expect(business and all(row["stored"] == row["published"] for row in business), "rows differ")
	failed = [row for row in document["metrics"] if row["passed"] is False]
	expect(not failed, f"failing metrics: {failed[:3]}")
	timed = {row["rule"] for row in document["metrics"] if row["metric"] == "Migration time, seconds"}
	expect(set(work) - {"stage"} <= timed, f"times in the metrics: {timed}")
	names = {row["metric"] for row in document["metrics"]}
	expect(("Integrity rules broken" in names) == (route == "Set-based SQL"), f"metrics of a {route} run")
	return current, shown, document


def _summary(current, shown: dict, document: dict) -> dict:
	return {
		"run": current.name,
		"display_id": current.display_id,
		"state": current.state,
		"route": shown["facts"]["route"],
		"steps": {step["step"]: [step["status"], step["seconds"]] for step in shown["steps"]},
		"list_duration": current.duration,
		"metrics": len(document["metrics"]),
		"rows_published": sum(row["published"] for row in shown["records"]),
	}


def ready(run: str) -> dict:
	current, shown, document = _loaded(run)
	expect(current.state == "Publication ready", f"the run is {current.state}")
	expect(_step(shown, "activate")["status"] == "decision", "the run does not wait for the decision")
	held = [row for row in document["metrics"] if row["metric"] == "Access rows held for activation"]
	deferred = [row for row in shown["records"] if row["deferred"]]
	expect(all(row["stored"] is None for row in deferred), "access rows are counted before activation")
	expect(len(held) == len([row for row in deferred if row["doctype"] in _published(current)]), "held rows")
	return {**_summary(current, shown, document), "access_tables_held": len(held)}


def _published(current) -> dict:
	return json.loads(current.reconciliation_json).get("published_rows") or {}


def activated(run: str) -> dict:
	current, shown, document = _loaded(run)
	report = json.loads(current.reconciliation_json)
	expect(current.state == "Activated", f"the run is {current.state}")
	status = frappe.db.get_value("Context", current.context_id, "context_status")
	expect(status == "Active", f"the delegation's context is {status}")
	step = _step(shown, "activate")
	expect(step["status"] == "done" and step["seconds"] is not None, f"activation step: {step}")
	facts = step["facts"]
	expect(facts["by"] == current.approved_by and facts["on"], f"who and when: {facts}")
	expect(facts["released"] == (report.get("released_access_rows") or {}), "released rows differ")
	expect("discard" not in [step["step"] for step in shown["steps"]], "an activated run shows a discard")
	# an access row is released or, for a share to a delegation not yet live, held: none is lost
	published = _published(current)
	for row in shown["records"]:
		if row["deferred"] and row["doctype"] in published:
			expect(row["stored"] is not None, f"{row['doctype']} is not counted after activation")
			expect(row["stored"] + row["held"] == row["published"], f"{row['doctype']}: {row}")
	names = {(row["metric"], row["entity"]) for row in document["metrics"]}
	expect(not any(metric == "Access rows held for activation" for metric, _ in names), "rows still held")
	for doctype in ("ToDo", "Record Share"):
		expect(doctype not in published or ("Number of records", doctype) in names, f"{doctype} not counted")
	timed = {
		row["rule"]: row["value"] for row in document["metrics"] if row["metric"] == "Migration time, seconds"
	}
	expect(timed.get("activate") == step["seconds"], f"activation time in the metrics: {timed}")
	work = sum(step["seconds"] or 0 for step in shown["steps"] if step["step"] != "discard")
	expect(abs((current.duration or 0) - work) <= 1, f"list duration {current.duration}, steps {work}")
	return {
		**_summary(current, shown, document),
		"activated_by": facts["by"],
		"activated_on": facts["on"],
		"activation_seconds": step["seconds"],
		"released": facts["released"],
		"shares_held": facts["held"],
		"hand_over": _step(shown, "hand over").get("status"),
	}


def main() -> None:
	parser = argparse.ArgumentParser(
		description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
	)
	parser.add_argument("command", choices=["findings", "refused", "handover", "ready", "activated"])
	parser.add_argument("--site", required=True)
	parser.add_argument("--run", required=True)
	parser.add_argument("--publication", type=Path)
	parser.add_argument("--sample-every", type=int, default=50)
	parser.add_argument("--quality-config", type=Path, help="PyMySQL connection JSON of a stand-in database")
	parser.add_argument("--quality-table", default="ucm_target_metric_rehearsal")
	parser.add_argument("--keep-table", action="store_true")
	parser.add_argument("--evidence", type=Path)
	args = parser.parse_args()
	evidence = args.evidence.resolve() if args.evidence else None
	publication = args.publication.resolve() if args.publication else None
	config = args.quality_config.resolve() if args.quality_config else None
	sys.path.insert(0, str(Path(__file__).resolve().parent))
	connect(args.site)
	try:
		if args.command == "findings":
			result = findings(args.run, publication, args.sample_every)
		elif args.command == "refused":
			result = refused(args.run, publication)
		elif args.command == "ready":
			result = ready(args.run)
		elif args.command == "activated":
			result = activated(args.run)
		else:
			result = handover(args.site, args.run, config, args.quality_table, args.keep_table)
	finally:
		frappe.destroy()
	text = json.dumps(result, indent=1, default=str)
	print(text)
	if evidence:
		evidence.write_text(text + "\n")


if __name__ == "__main__":
	main()
