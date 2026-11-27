"""The target's side of the data quality reconciliation: what a run's verification found, as metric rows the
migration side can put beside its own.

The UCM site connects to nothing on the migration side. The push command asks for these rows over HTTPS, writes
them where the migration team keeps its metrics, and tells the run that it did. The rows hold counts and rule
names, never a record's key or a field's value."""

FORMAT = "ucm-target-metrics-v1"
CRITICAL, HIGH, MEDIUM, LOW = "CRITICAL", "HIGH", "MEDIUM", "LOW"
SEVERITIES = (CRITICAL, HIGH, MEDIUM, LOW)
COMPLETENESS = "Completeness"
ACCURACY = "Field-level accuracy"
INTEGRITY = "Referential integrity"
OVERALL = "Cross-cutting"
COLUMNS = (
	"run",
	"site",
	"context",
	"context_code",
	"snapshot",
	"route",
	"state",
	"generated_on",
	"category",
	"metric",
	"entity",
	"rule",
	"source_value",
	"target_value",
	"value",
	"target",
	"severity",
	"passed",
)
DEFERRED = ("ToDo", "Record Share")  # held back until the delegation is activated

# A rule's name ends with what it checks (set_based._rules). A link that points nowhere or out of the delegation
# is a broken relationship; a value outside its list is a wrong value set; a default left empty changes nothing
# a user would see once the record is saved again.
_RULE_SEVERITY = (
	("link to nothing", CRITICAL),
	("belongs to another context", CRITICAL),
	("not this delegation", CRITICAL),
	("empty where a save sets its default", MEDIUM),
)


def rule_severity(rule: str) -> str:
	"""The severity of a broken integrity rule, on the scale of the migration's quality metrics."""
	for ending, severity in _RULE_SEVERITY:
		if rule.endswith(ending):
			return severity
	return HIGH


def findings(report: dict | None) -> list[dict]:
	"""A verification's findings, one row per check that found something: the table, what was checked, how many
	rows, how severe, and the first place (batch and item) where the publication says so."""
	found = (report or {}).get("findings") or {}
	first = (report or {}).get("first_place") or {}
	rows = []
	for doctype, (stored, published) in (found.get("row counts") or {}).items():
		check = "rows missing in UCM" if stored < published else "rows in UCM that were not published"
		rows.append(_finding(doctype, check, abs(published - stored), CRITICAL))
	for doctype, number in (found.get("rows missing") or {}).items():
		rows.append(_finding(doctype, "published rows not found by their key", number, CRITICAL))
	for key, number in (found.get("fields differing") or {}).items():
		doctype, _dot, field = key.partition(".")
		rows.append(
			_finding(doctype, "field differs from the publication", number, CRITICAL, field, first.get(key))
		)
	for doctype, rules in (found.get("rules broken") or {}).items():
		for rule, number in rules.items():
			rows.append(_finding(doctype, "rule broken", number, rule_severity(rule), rule))
	if found.get("receipts"):
		rows.append(_finding("", "batch receipts differ from the publication", 1, CRITICAL))
	order = {severity: position for position, severity in enumerate(SEVERITIES)}
	return sorted(rows, key=lambda row: (order[row["severity"]], row["doctype"], row["check"], row["detail"]))


def _finding(doctype, check, found, severity, detail="", place=None) -> dict:
	return {
		"doctype": doctype,
		"check": check,
		"detail": detail,
		"found": int(found),
		"severity": severity,
		"batch": place[0] if place else None,
		"item": place[1] if place else None,
	}


def _row(
	category,
	metric,
	entity="",
	rule="",
	source=None,
	target=None,
	value=None,
	goal="",
	severity="",
	passed=None,
):
	return {
		"category": category,
		"metric": metric,
		"entity": entity,
		"rule": rule,
		"source_value": source,
		"target_value": target,
		"value": value,
		"target": goal,
		"severity": severity,
		"passed": passed,
	}


def _stored(report: dict) -> dict:
	"""The rows of each table on the target: counted (set-based route) or read back one by one (documents)."""
	return report.get("stored_rows") or report.get("checked_business_rows") or {}


def _completeness(report: dict, activated: bool) -> list[dict]:
	rows, stored = [], _stored(report)
	released = report.get("released_access_rows") or {}
	held = report.get("shares_held_at_activation") or 0
	for entity, published in sorted((report.get("published_rows") or {}).items()):
		if entity in DEFERRED:
			if not activated:
				rows.append(
					_row(
						COMPLETENESS,
						"Access rows held for activation",
						entity,
						source=published,
						severity=LOW,
					)
				)
				continue
			# a share to a delegation that is not live yet is kept as a receipt and released when it arrives
			target = released.get(entity, 0) + (held if entity == "Record Share" else 0)
		elif entity not in stored:
			continue
		else:
			target = stored[entity]
		ratio = 1.0 if published == target else (round(target / published, 6) if published else None)
		same = published == target
		rows.append(
			_row(
				COMPLETENESS,
				"Number of records",
				entity,
				"",
				published,
				target,
				ratio,
				"100%",
				CRITICAL,
				same,
			)
		)
		rows.append(
			_row(
				COMPLETENESS,
				"Lost records",
				entity,
				"",
				published,
				target,
				published - target,
				"0",
				CRITICAL,
				same,
			)
		)
	return rows


def _accuracy(report: dict, found: list[dict]) -> list[dict]:
	rows = []
	differing = {}
	for finding in found:
		if finding["check"] in (
			"field differs from the publication",
			"published rows not found by their key",
		):
			differing[finding["doctype"]] = differing.get(finding["doctype"], 0) + finding["found"]
	for entity, compared in sorted((report.get("rows_compared") or {}).items()):
		bad = differing.get(entity, 0)
		rows.append(
			_row(
				ACCURACY,
				"Exact match per field",
				entity,
				"",
				compared,
				None,
				bad,
				"0 differences",
				CRITICAL,
				not bad,
			)
		)
	if report.get("batch_chain_sha256"):
		chained = not any(finding["check"].startswith("batch receipts") for finding in found)
		rows.append(
			_row(
				ACCURACY,
				"Hash exact match",
				"",
				"batch receipt chain",
				goal="equal",
				severity=CRITICAL,
				passed=chained,
			)
		)
	return rows


def _integrity(report: dict, found: list[dict]) -> list[dict]:
	broken = [finding for finding in found if finding["check"] == "rule broken"]
	rows = [
		_row(
			INTEGRITY,
			"Integrity rule broken",
			f["doctype"],
			f["detail"],
			value=f["found"],
			goal="0",
			severity=f["severity"],
			passed=False,
		)
		for f in broken
	]
	if "rules_checked" in report:
		rows.append(
			_row(
				INTEGRITY,
				"Integrity rules broken",
				source=report["rules_checked"],
				value=len(broken),
				goal="0",
				severity=CRITICAL,
				passed=not broken,
			)
		)
	if "sampled_document_checks" in report:
		# a sampled record that fails stops the run at that record, so a finished verification passed them all
		rows.append(
			_row(
				INTEGRITY,
				"Sampling validation",
				value=report["sampled_document_checks"],
				goal="100%",
				severity=CRITICAL,
				passed=True,
			)
		)
	return rows


def metrics(run: dict, report: dict | None, steps: list[dict] | None = None) -> dict:
	"""The metric rows of one run. `run`: its identity (run, site, context, context_code, snapshot, route, state,
	generated_on). `report`: what its verification kept. `steps`: the run's steps with their seconds."""
	report = report or {}
	found = findings(report)
	rows = (
		_completeness(report, run.get("state") == "Activated")
		+ _accuracy(report, found)
		+ _integrity(report, found)
	)
	failed = {severity: 0 for severity in SEVERITIES}
	for row in rows:
		if row["passed"] is False:
			failed[row["severity"]] += 1
	for severity in SEVERITIES:
		rows.append(
			_row(
				OVERALL,
				"Errors count by severity",
				"",
				severity,
				value=failed[severity],
				goal="0",
				severity=severity,
				passed=not failed[severity],
			)
		)
	for step in steps or []:
		if step.get("seconds") is not None:
			rows.append(
				_row(
					OVERALL,
					"Migration time, seconds",
					"",
					step["step"],
					value=step["seconds"],
					severity=MEDIUM,
				)
			)
	return {"format": FORMAT, **run, "metrics": [{**run, **row} for row in rows]}


def as_csv(document: dict) -> str:
	"""The metric rows as CSV, with the run's identity on every row."""
	import csv
	import io

	out = io.StringIO()
	writer = csv.DictWriter(out, COLUMNS, lineterminator="\n")
	writer.writeheader()
	for row in document["metrics"]:
		writer.writerow({column: _cell(row.get(column)) for column in COLUMNS})
	return out.getvalue()


def _cell(value):
	if value is None:
		return ""
	if isinstance(value, bool):
		return int(value)
	return value


# ---------------------------------------------------------------------------------------------------------------
# on the migration side

TABLE = "ucm_target_metric"
DDL = """create table if not exists `{table}` (
	run char(64) not null,
	site varchar(140) not null,
	context varchar(140) not null,
	context_code varchar(140),
	snapshot varchar(140) not null,
	route varchar(60),
	state varchar(60),
	generated_on datetime(6) not null,
	category varchar(60) not null,
	metric varchar(80) not null,
	entity varchar(140) not null default '',
	rule varchar(190) not null default '',
	source_value decimal(30, 6),
	target_value decimal(30, 6),
	value decimal(30, 6),
	target varchar(60),
	severity varchar(20),
	passed tinyint,
	primary key (run, category, metric, entity, rule)
)"""


def store(connection, document: dict, table: str = TABLE, create: bool = False) -> int:
	"""Write a run's metric rows into a table of the migration side's database, replacing the rows the same run
	left before, in one transaction. `connection`: a DB-API connection (PyMySQL). Returns the rows written."""
	import re

	if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", table):
		raise ValueError("Name the metrics table with letters, digits and underscores")
	rows = [
		tuple(_cell(row.get(column)) if row.get(column) is not None else None for column in COLUMNS)
		for row in document["metrics"]
	]
	if any(row[0] != document["run"] for row in rows):
		raise ValueError("A metric row belongs to another run")
	columns = ", ".join(f"`{column}`" for column in COLUMNS)
	marks = ", ".join(["%s"] * len(COLUMNS))
	try:
		with connection.cursor() as cursor:
			if create:
				cursor.execute(DDL.format(table=table))
			# the table's name was checked above; every value is a parameter
			cursor.execute(f"delete from `{table}` where run = %s", (document["run"],))
			cursor.executemany(f"insert into `{table}` ({columns}) values ({marks})", rows)
		connection.commit()
	except Exception:
		connection.rollback()
		raise
	return len(rows)
