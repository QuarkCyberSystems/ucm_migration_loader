"""The parts of a run's overview and of its data quality metrics that need no site: the steps a run's records
give, the rows on both sides, the findings with their severity, and the rows written on the migration side."""

import csv
import io
import json

import pytest

from ucm_migration_loader.ucm_migration_loader import data_quality, run_overview

RUN = "a" * 64
REPORT = {
	"route": "Set-based SQL",
	"published_rows": {"Person": 100, "Activity": 300, "ToDo": 4, "Record Share": 10},
	"stored_rows": {"Person": 100, "Activity": 300, "ToDo": 0, "Record Share": 0},
	"rows_compared": {"Person": 100, "Activity": 300},
	"derived_rows_changed": {"place geography": 7, "share facts": 0},
	"rules_checked": 40,
	"sampled_document_checks": 8,
	"deferred_access_rows": {"ToDo": 4, "Record Share": 10},
	"batch_chain_sha256": "b" * 64,
	"committed_business_batches": 4,
	"findings": {},
	"first_place": {},
	"seconds": {
		"count": 1.0,
		"integrity rules": 2.0,
		"compare with the publication": 3.0,
		"sampled document checks": 4.0,
	},
}
FOUND = {
	**REPORT,
	"stored_rows": {"Person": 99, "Activity": 300, "ToDo": 0, "Record Share": 0},
	"findings": {
		"row counts": {"Person": [99, 100]},
		"rows missing": {"Person": 1},
		"fields differing": {"Activity.activity_tags": 3},
		"rules broken": {
			"Activity": {
				"activity_type_tech_id: link to nothing": 2,
				"activity_status: not an option": 5,
				"activity_language: empty where a save sets its default": 1,
			}
		},
	},
	"first_place": {"Activity.activity_tags": [148, 45]},
}
RECEIPTS = [
	{
		"write_route": "Set-based SQL",
		"batches": 3,
		"parent_rows": 400,
		"write_seconds": 20.0,
		"validation_seconds": None,
		"attachment_seconds": None,
		"verify_seconds": None,
		"reconcile_seconds": None,
	},
	{
		"write_route": "Document API",
		"batches": 1,
		"parent_rows": 6,
		"write_seconds": 5.0,
		"validation_seconds": 0.5,
		"attachment_seconds": 0,
		"verify_seconds": 0,
		"reconcile_seconds": 0,
	},
]


def run(state, route="Set-based SQL", **more):
	return {
		"state": state,
		"route": route,
		"batches": 6,
		"rows": 414,
		"tables": 21,
		"reconciled": 0,
		"activated_by": None,
		"activated_on": None,
		**more,
	}


def statuses(steps):
	return {step["step"]: step["status"] for step in steps}


def test_route_of_a_plan():
	assert run_overview.route_of({}) == "Document API"
	assert run_overview.route_of({"Person": "Set-based SQL", "Case": "Set-based SQL"}) == "Set-based SQL"
	assert run_overview.route_of({"Person": "Document API", "Case": "Validated bulk SQL"}) == "By DocType"


def test_a_staged_run_waits_at_every_step():
	steps = run_overview.steps(run("Publication staged"), [], {}, {}, {"stage": {"seconds": 12.34}}, {})
	assert statuses(steps) == {
		"stage": "done",
		"write": "waiting",
		"derive": "waiting",
		"verify": "waiting",
		"activate": "waiting",
	}
	assert steps[0]["seconds"] == 12.3 and steps[0]["facts"] == {"batches": 6, "rows": 414, "tables": 21}
	assert steps[1]["seconds"] is None


def test_an_approved_run_waits_for_its_file():
	approval = {"by": "admin@example.org", "publisher": "sender@example.org", "bytes": 300, "arrived": 0}
	waiting = run_overview.steps({**run("Awaiting publication"), "approval": approval}, [], {}, {}, {}, {})
	assert statuses(waiting) == {
		"stage": "waiting",
		"write": "waiting",
		"verify": "waiting",
		"activate": "waiting",
	}
	assert waiting[0]["facts"] == approval
	arriving = {**approval, "arrived": 120}
	steps = run_overview.steps({**run("Awaiting publication"), "approval": arriving}, [], {}, {}, {}, {})
	assert statuses(steps)["stage"] == "running" and steps[0]["facts"]["arrived"] == 120


class Record(dict):
	__getattr__ = dict.get


def test_a_run_names_who_proposed_approved_and_staged_it():
	approval = {
		"proposed_by": "sender@example.org",
		"proposed_on": "2026-01-05 09:00:00",
		"by": "admin@example.org",
		"on": "2026-01-05 10:00:00",
	}
	timeline = {"stage": {"seconds": 2, "at": "2026-01-05 11:00:00", "by": "admin@example.org"}}
	record = Record(owner="sender@example.org", creation="2026-01-05 09:00:00", state="Proposed")
	record["approval_json"] = json.dumps({key: approval[key] for key in ("proposed_by", "proposed_on")})
	proposed = run_overview._people(record)
	assert (proposed["proposed_by"], proposed["proposed_on"]) == ("sender@example.org", "2026-01-05 09:00:00")
	assert not any(proposed[key] for key in ("approved_by", "approved_on", "staged_by", "staged_on"))

	record.update(state="Publication staged", approval_json=json.dumps(approval))
	record["timeline_json"] = json.dumps(timeline)
	staged = run_overview._people(record)
	assert (staged["approved_by"], staged["approved_on"]) == ("admin@example.org", "2026-01-05 10:00:00")
	assert (staged["staged_by"], staged["staged_on"]) == ("admin@example.org", "2026-01-05 11:00:00")
	assert staged["proposed_by"] == "sender@example.org"


def test_a_run_made_by_its_staging_names_its_author():
	record = Record(owner="admin@example.org", creation="2026-01-05 11:00:00", state="Publication staged")
	record["timeline_json"] = json.dumps({"stage": {"seconds": 2, "at": "2026-01-05 11:00:03"}})
	people = run_overview._people(record)
	assert (people["staged_by"], people["staged_on"]) == ("admin@example.org", "2026-01-05 11:00:00")
	assert people["approved_by"] is None and people["proposed_by"] is None


def test_a_running_load_shows_the_step_it_is_at():
	timeline = {"stage": {"seconds": 5, "business_batches": 4}}
	writing = run_overview.steps(
		run("Publication applying"), RECEIPTS[:1], {"route": "Set-based SQL"}, {}, timeline, {}
	)
	assert statuses(writing)["write"] == "running" and statuses(writing)["derive"] == "waiting"
	deriving = run_overview.steps(
		run("Publication applying"), RECEIPTS, {"route": "Set-based SQL"}, {}, timeline, {}
	)
	assert statuses(deriving) == {
		"stage": "done",
		"write": "done",
		"derive": "running",
		"verify": "waiting",
		"activate": "waiting",
	}
	progress = {
		"derived": {"place geography": 7},
		"derive_seconds": 9.0,
		"compared": {"seconds": {"compare": 2.0, "sample": 1.0}},
	}
	verifying = run_overview.steps(
		run("Publication applying", reconciled=2), RECEIPTS, progress, {}, timeline, {}
	)
	assert statuses(verifying)["derive"] == "done" and statuses(verifying)["verify"] == "running"
	verify = verifying[3]
	assert (
		verify["seconds"] == 3.0 and verify["facts"]["reconciled"] == 2 and verify["facts"]["business"] == 4
	)
	paused = run_overview.steps(
		run("Publication paused"), RECEIPTS[:1], {"route": "Set-based SQL"}, {}, timeline, {}
	)
	assert statuses(paused)["write"] == "paused"


def test_a_verified_run_waits_for_the_decision():
	progress = {"derived": REPORT["derived_rows_changed"], "derive_seconds": 9.0}
	steps = run_overview.steps(
		run("Publication ready"), RECEIPTS, progress, REPORT, {"stage": {"seconds": 5}}, {}
	)
	assert statuses(steps) == {
		"stage": "done",
		"write": "done",
		"derive": "done",
		"verify": "done",
		"hand over": "waiting",
		"activate": "decision",
	}
	write, _derive, verify = steps[1:4]
	assert write["seconds"] == 25.5 and write["facts"] == {
		"batches": 4,
		"parents": 406,
		"by_route": {"Set-based SQL": 400, "Document API": 6},
	}
	assert verify["seconds"] == 10.0 and verify["facts"]["rows"] == 400 and verify["facts"]["findings"] == 0
	assert steps[-1]["facts"] == {"deferred": {"ToDo": 4, "Record Share": 10}}
	assert run_overview.duration(steps) == pytest.approx(5 + 25.5 + 9 + 10)


def test_findings_fail_the_verification_and_keep_the_run_from_the_decision():
	steps = run_overview.steps(run("Publication paused"), RECEIPTS, {"route": "Set-based SQL"}, FOUND, {}, {})
	assert statuses(steps)["verify"] == "failed" and statuses(steps)["activate"] == "waiting"
	assert steps[3]["facts"]["findings"] == 6
	# the progress of a failed verification is reset for the next one: the report keeps how long deriving took
	kept = run_overview.steps(
		run("Publication paused"),
		RECEIPTS,
		{"route": "Set-based SQL"},
		{**FOUND, "derive_seconds": 9.0},
		{},
		{},
	)
	assert kept[2]["step"] == "derive" and kept[2]["status"] == "done" and kept[2]["seconds"] == 9.0


def test_activation_hand_over_and_discard_close_the_list():
	handover = {
		"rows": 20,
		"sha256": "c" * 64,
		"destination": "table ucm_target_metric",
		"by": "publisher",
		"on": "now",
	}
	report = {
		**REPORT,
		"released_access_rows": {"ToDo": 4, "Record Share": 6},
		"shares_held_at_activation": 4,
	}
	activated = run("Activated", activated_by="operator", activated_on="2026-01-01 10:00:00")
	steps = run_overview.steps(activated, RECEIPTS, {}, report, {"activate": {"seconds": 2.04}}, handover)
	assert statuses(steps)["hand over"] == "done" and steps[-2]["facts"] == handover
	assert steps[-1]["step"] == "activate" and steps[-1]["seconds"] == 2.0 and steps[-1]["facts"]["held"] == 4
	gone = run_overview.steps(run("Discarded"), RECEIPTS[:1], {}, {}, {"discard": {"seconds": 30}}, {})
	assert [step["step"] for step in gone] == ["stage", "write", "derive", "verify", "discard"]
	assert statuses(gone)["write"] == "stopped" and gone[-1]["status"] == "done" and gone[-1]["seconds"] == 30
	assert run_overview.duration(gone) == 20.0
	cleaning = run_overview.steps(run("Publication cleaning"), [], {}, {}, {}, {})
	assert cleaning[-1]["status"] == "running"


def test_a_hand_over_of_earlier_metrics_is_due_again():
	handover = {"rows": 12, "destination": "file metrics.csv", "on": "2026-01-01 00:00:00", "current": False}
	steps = run_overview.steps(run("Activated"), RECEIPTS, {}, REPORT, {}, handover)
	(step,) = [step for step in steps if step["step"] == "hand over"]
	assert step["status"] == "waiting" and step["facts"]["on"] == "2026-01-01 00:00:00"


def test_the_document_route_has_no_derive_step():
	report = {
		"published_rows": {"Person": 5},
		"checked_business_rows": {"Person": 5},
		"committed_business_batches": 1,
	}
	receipts = [{**RECEIPTS[1], "reconcile_seconds": 7.0}]
	steps = run_overview.steps(run("Publication ready", "Document API"), receipts, {}, report, {}, {})
	assert [step["step"] for step in steps] == ["stage", "write", "verify", "hand over", "activate"]
	assert steps[2]["seconds"] == 7.0 and steps[2]["facts"]["rows"] == 5


def test_a_refused_publication_shows_what_differs():
	preflight = {"schema_differences": ["Case"], "release_differences": {}, "existing_keys": {"Person": 3}}
	steps = run_overview.steps(
		run("Rejected"), [], {}, {"preflight": preflight}, {"stage": {"seconds": 1.5}}, {}
	)
	assert steps == [{"step": "stage", "status": "failed", "seconds": 1.5, "facts": preflight}]


def test_a_refused_publication_says_who_removed_its_file():
	preflight = {"schema_differences": ["Case"], "release_differences": {}, "existing_keys": {}}
	removed = {"removed_by": "admin@example.org", "removed_on": "2026-11-28 10:00:00"}
	approval = {"by": "admin@example.org", "publisher": "sender@example.org", **removed}
	steps = run_overview.steps(
		run("Rejected", approval=approval), [], {}, {"preflight": preflight}, {"stage": {"seconds": 1.5}}, {}
	)
	assert steps[0]["facts"] == {**preflight, **removed}


def test_records_show_both_sides_and_what_activation_released():
	hidden = {row["doctype"]: row for row in run_overview.records(REPORT["published_rows"], REPORT, False)}
	assert (
		hidden["Person"]["stored"] == 100 and hidden["ToDo"]["stored"] is None and hidden["ToDo"]["deferred"]
	)
	report = {
		**REPORT,
		"released_access_rows": {"ToDo": 4, "Record Share": 6},
		"shares_held_at_activation": 4,
	}
	live = {row["doctype"]: row for row in run_overview.records(REPORT["published_rows"], report, True)}
	assert (live["Record Share"]["stored"], live["Record Share"]["held"]) == (6, 4)
	assert (live["ToDo"]["stored"], live["ToDo"]["held"]) == (4, 0)
	assert run_overview.records({"Person": 5}, {}, False) == [
		{"doctype": "Person", "published": 5, "stored": None, "held": 0, "deferred": False}
	]


def test_findings_carry_a_severity_and_a_place_but_no_value():
	findings = data_quality.findings(FOUND)
	assert [(row["severity"], row["doctype"], row["check"]) for row in findings] == [
		("CRITICAL", "Activity", "field differs from the publication"),
		("CRITICAL", "Activity", "rule broken"),
		("CRITICAL", "Person", "published rows not found by their key"),
		("CRITICAL", "Person", "rows missing in UCM"),
		("HIGH", "Activity", "rule broken"),
		("MEDIUM", "Activity", "rule broken"),
	]
	differing = findings[0]
	assert (differing["detail"], differing["found"], differing["batch"], differing["item"]) == (
		"activity_tags",
		3,
		148,
		45,
	)
	assert set(differing) == {"doctype", "check", "detail", "found", "severity", "batch", "item"}
	assert data_quality.findings({}) == [] and data_quality.findings(None) == []
	extra = data_quality.findings({"findings": {"row counts": {"Case": [7, 5]}, "receipts": ["differ"]}})
	assert [row["check"] for row in extra] == [
		"batch receipts differ from the publication",
		"rows in UCM that were not published",
	]


def test_rule_severity_follows_what_the_rule_checks():
	assert data_quality.rule_severity("person_tech_id: link to nothing") == "CRITICAL"
	assert data_quality.rule_severity("case_tech_id: belongs to another context") == "CRITICAL"
	assert data_quality.rule_severity("context_tech_id: not this delegation") == "CRITICAL"
	assert data_quality.rule_severity("gender: not an active value of its list") == "HIGH"
	assert data_quality.rule_severity("person_name: required") == "HIGH"
	assert data_quality.rule_severity("language: empty where a save sets its default") == "MEDIUM"


IDENTITY = {
	"run": RUN,
	"site": "ucm.example",
	"context": "ctx",
	"context_code": "ABC",
	"snapshot": "s1",
	"route": "Set-based SQL",
	"state": "Publication ready",
	"generated_on": "2026-01-01 10:00:00",
}


def by_metric(document):
	return {(row["metric"], row["entity"], row["rule"]): row for row in document["metrics"]}


def test_metrics_of_a_clean_run_all_pass():
	steps = [{"step": "write", "seconds": 25.5}, {"step": "activate", "seconds": None}]
	document = data_quality.metrics(IDENTITY, REPORT, steps)
	assert document["format"] == "ucm-target-metrics-v1" and document["run"] == RUN
	rows = by_metric(document)
	assert all(row["run"] == RUN and row["context_code"] == "ABC" for row in document["metrics"])
	assert not [row for row in document["metrics"] if row["passed"] is False]
	number = rows["Number of records", "Person", ""]
	assert (number["source_value"], number["target_value"], number["value"], number["severity"]) == (
		100,
		100,
		1.0,
		"CRITICAL",
	)
	assert rows["Lost records", "Activity", ""]["value"] == 0
	# access rows are held until activation: counted as published, not as lost
	assert rows["Access rows held for activation", "Record Share", ""]["source_value"] == 10
	assert ("Number of records", "Record Share", "") not in rows
	assert rows["Integrity rules broken", "", ""]["source_value"] == 40
	assert rows["Sampling validation", "", ""]["value"] == 8
	assert rows["Hash exact match", "", "batch receipt chain"]["passed"] is True
	assert rows["Errors count by severity", "", "CRITICAL"]["value"] == 0
	assert rows["Migration time, seconds", "", "write"]["value"] == 25.5
	assert ("Migration time, seconds", "", "activate") not in rows


def test_metrics_count_what_a_verification_found():
	rows = by_metric(data_quality.metrics({**IDENTITY, "state": "Publication paused"}, FOUND))
	person = rows["Number of records", "Person", ""]
	assert (person["target_value"], person["value"], person["passed"]) == (99, 0.99, False)
	assert rows["Lost records", "Person", ""]["value"] == 1
	assert rows["Exact match per field", "Activity", ""]["value"] == 3
	assert rows["Exact match per field", "Person", ""]["passed"] is False
	broken = rows["Integrity rule broken", "Activity", "activity_status: not an option"]
	assert (broken["value"], broken["severity"], broken["passed"]) == (5, "HIGH", False)
	assert rows["Integrity rules broken", "", ""]["value"] == 3
	assert rows["Errors count by severity", "", "CRITICAL"]["value"] == 6
	assert rows["Errors count by severity", "", "HIGH"]["value"] == 1
	assert rows["Errors count by severity", "", "MEDIUM"]["passed"] is False


def test_metrics_after_activation_count_released_and_held_access_rows():
	report = {
		**REPORT,
		"released_access_rows": {"ToDo": 4, "Record Share": 6},
		"shares_held_at_activation": 4,
	}
	rows = by_metric(data_quality.metrics({**IDENTITY, "state": "Activated"}, report))
	assert rows["Number of records", "Record Share", ""]["target_value"] == 10
	assert rows["Number of records", "ToDo", ""]["passed"] is True
	lost = by_metric(
		data_quality.metrics({**IDENTITY, "state": "Activated"}, {**report, "shares_held_at_activation": 3})
	)
	assert lost["Lost records", "Record Share", ""]["value"] == 1


def test_csv_has_the_run_on_every_row():
	document = data_quality.metrics(IDENTITY, REPORT)
	table = list(csv.DictReader(io.StringIO(data_quality.as_csv(document))))
	assert len(table) == len(document["metrics"]) and list(table[0]) == list(data_quality.COLUMNS)
	assert {row["run"] for row in table} == {RUN}
	first = table[0]
	assert (first["metric"], first["passed"], first["rule"]) == ("Number of records", "1", "")


class Cursor:
	def __init__(self, log):
		self.log = log

	def __enter__(self):
		return self

	def __exit__(self, *exc):
		return False

	def execute(self, sql, values=None):
		if "fail" in self.log:
			raise RuntimeError("database refused")
		self.log.append(("execute", " ".join(sql.split())[:40], values))

	def executemany(self, sql, rows):
		self.log.append(("executemany", " ".join(sql.split())[:40], len(rows)))


class Connection:
	def __init__(self, *log):
		self.log = list(log)

	def cursor(self):
		return Cursor(self.log)

	def commit(self):
		self.log.append("commit")

	def rollback(self):
		self.log.append("rollback")


def test_store_replaces_a_runs_rows_in_one_transaction():
	document = data_quality.metrics(IDENTITY, REPORT)
	connection = Connection()
	assert data_quality.store(connection, document, "dq_target", create=True) == len(document["metrics"])
	assert connection.log == [
		("execute", "create table if not exists `dq_target` (", None),
		("execute", "delete from `dq_target` where run = %s", (RUN,)),
		("executemany", "insert into `dq_target` (`run`, `site`, ", len(document["metrics"])),
		"commit",
	]
	plain = Connection()
	data_quality.store(plain, document)
	assert plain.log[0][1] == "delete from `ucm_target_metric` where ru"


def test_store_refuses_an_unsafe_table_and_rolls_back_a_failure():
	document = data_quality.metrics(IDENTITY, REPORT)
	with pytest.raises(ValueError, match="letters, digits and underscores"):
		data_quality.store(Connection(), document, "dq`; drop table x")
	foreign = {**document, "metrics": [{**document["metrics"][0], "run": "b" * 64}]}
	with pytest.raises(ValueError, match="another run"):
		data_quality.store(Connection(), foreign)
	failing = Connection("fail")
	with pytest.raises(RuntimeError):
		data_quality.store(failing, document)
	assert failing.log == ["fail", "rollback"]
