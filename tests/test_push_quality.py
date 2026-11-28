"""`ucm-push quality` on the migration side, with the site's answers played back: the metrics it accepts, where
it writes them, and what it tells the run. No site and no network."""

import csv
import json
import sys

import pytest

from ucm_migration_loader import push
from ucm_migration_loader.contract import digest
from ucm_migration_loader.ucm_migration_loader import data_quality

RUN = "a" * 64
SITE = "target.example"
IDENTITY = {
	"run": RUN,
	"site": SITE,
	"context": "11111111-1111-4111-8111-111111111111",
	"context_code": "TST",
	"snapshot": "TEST-1",
	"route": "Set-based SQL",
	"state": "Publication paused",
	"generated_on": "2026-01-02 03:04:05.000000",
}
REPORT = {
	"published_rows": {"Person": 100},
	"stored_rows": {"Person": 99},
	"rows_compared": {"Person": 99},
	"rules_checked": 40,
	"findings": {"row counts": {"Person": [99, 100]}},
}


def answer() -> dict:
	"""What the site answers to `quality`: the metric rows and their digest."""
	document = data_quality.metrics(IDENTITY, REPORT)
	return {**document, "sha256": digest(document["metrics"])}


class Site:
	"""The target site as the push command meets it: one HTTPS session, whose requests are kept."""

	def __init__(self, quality=None, status=200):
		self.quality = answer() if quality is None else quality
		self.status = status
		self.requests = []

	def mount(self, *_args):
		pass

	def post(self, url, json, headers, timeout, allow_redirects):
		method = url.rsplit(".", 1)[1]
		self.requests.append((method, url, json, headers, allow_redirects))
		site = self

		class Response:
			status_code = site.status

			def json(self):
				if method == "quality":
					return {"message": site.quality}
				return {"message": {**json, "by": "publisher@example.org", "on": "2026-01-02 04:00:00"}}

		return Response()

	def sent(self, method):
		return [request[2] for request in self.requests if request[0] == method]


class Database:
	"""The migration side's database: a connection that keeps what was executed on it."""

	def __init__(self, fail=False):
		self.fail = fail
		self.log = []
		self.config = None

	def connect(self, **config):
		self.config = config
		return self

	def __enter__(self):
		return self

	def __exit__(self, *_exc):
		self.log.append("close")

	def cursor(self):
		return Cursor(self)

	def commit(self):
		self.log.append("commit")

	def rollback(self):
		self.log.append("rollback")


class Cursor:
	def __init__(self, database):
		self.database = database

	def __enter__(self):
		return self

	def __exit__(self, *_exc):
		pass

	def execute(self, sql, values=None):
		if self.database.fail:
			raise RuntimeError("the database refused")
		self.database.log.append(sql.split("(")[0].strip())

	def executemany(self, sql, rows):
		self.database.log.append(("rows", len(rows)))


@pytest.fixture
def site(monkeypatch):
	site = Site()
	monkeypatch.setattr(push.requests, "Session", lambda: site)
	monkeypatch.setenv("UCM_MIGRATION_TOKEN", "key:secret")
	return site


def run(monkeypatch, capsys, *options, url="https://target.example"):
	monkeypatch.setattr(sys, "argv", ["ucm-push", "quality", "--site", SITE, "--url", url, *options])
	push.main()
	return json.loads(capsys.readouterr().out)


def test_the_metrics_are_written_as_csv_and_the_run_is_told(site, monkeypatch, capsys, tmp_path):
	out = tmp_path / "metrics.csv"
	result = run(monkeypatch, capsys, "--run", RUN, "--quality-out", str(out))
	rows = list(csv.DictReader(out.open()))
	assert len(rows) == len(site.quality["metrics"])
	assert {row["run"] for row in rows} == {RUN}
	assert tuple(rows[0]) == data_quality.COLUMNS
	(told,) = site.sent("quality_received")
	assert told == {
		"run": RUN,
		"sha256": site.quality["sha256"],
		"rows": len(rows),
		"destination": "file metrics.csv",
	}
	assert (result["run"], result["state"], result["metrics"]) == (RUN, "Publication paused", len(rows))
	assert result["failed"] == sum(row["passed"] == "0" for row in rows) > 0
	assert result["destination"] == "file metrics.csv"


def test_the_calls_go_to_the_loaders_api_over_https_with_the_token(site, monkeypatch, capsys, tmp_path):
	run(
		monkeypatch,
		capsys,
		"--run",
		RUN,
		"--quality-out",
		str(tmp_path / "m.csv"),
		url="https://target.example/",
	)
	assert [request[1] for request in site.requests] == [
		"https://target.example/api/method/ucm_migration_loader.api.quality",
		"https://target.example/api/method/ucm_migration_loader.api.quality_received",
	]
	assert all(request[3] == {"Authorization": "token key:secret"} for request in site.requests)
	assert not any(request[4] for request in site.requests)  # a redirect is never followed with the token


def test_another_suffix_gives_the_whole_document_as_json(site, monkeypatch, capsys, tmp_path):
	out = tmp_path / "metrics.json"
	run(monkeypatch, capsys, "--run", RUN, "--quality-out", str(out))
	assert json.loads(out.read_text()) == site.quality
	assert site.sent("quality_received")[0]["destination"] == "file metrics.json"


def test_the_metrics_go_to_the_table_and_the_run_learns_its_name_only(site, monkeypatch, capsys, tmp_path):
	database = Database()
	monkeypatch.setattr(push.pymysql, "connect", database.connect)
	config = tmp_path / "quality-db.json"
	config.write_text(
		json.dumps({"host": "dq.internal", "user": "dq", "password": "not-sent", "database": "dq"})
	)
	out = tmp_path / "metrics.csv"
	run(
		monkeypatch,
		capsys,
		"--run",
		RUN,
		"--quality-out",
		str(out),
		"--quality-config",
		str(config),
		"--quality-table",
		"dq_target",
		"--create-quality-table",
	)
	assert database.config["host"] == "dq.internal"
	assert database.log == [
		"create table if not exists `dq_target`",
		"delete from `dq_target` where run = %s",
		("rows", len(site.quality["metrics"])),
		"commit",
		"close",
	]
	(told,) = site.sent("quality_received")
	assert told["destination"] == "file metrics.csv, table dq_target"
	# where the database is, and how to enter it, stays on the migration side
	assert "dq.internal" not in json.dumps(site.requests, default=str)
	assert "not-sent" not in json.dumps(site.requests, default=str)


def test_the_default_table_is_not_created_unasked(site, monkeypatch, capsys, tmp_path):
	database = Database()
	monkeypatch.setattr(push.pymysql, "connect", database.connect)
	config = tmp_path / "quality-db.json"
	config.write_text("{}")
	run(monkeypatch, capsys, "--run", RUN, "--quality-config", str(config))
	assert database.log[0] == "delete from `ucm_target_metric` where run = %s"
	assert site.sent("quality_received")[0]["destination"] == "table ucm_target_metric"


def test_the_run_is_not_told_when_the_table_could_not_be_written(site, monkeypatch, capsys, tmp_path):
	database = Database(fail=True)
	monkeypatch.setattr(push.pymysql, "connect", database.connect)
	config = tmp_path / "quality-db.json"
	config.write_text("{}")
	with pytest.raises(RuntimeError, match="the database refused"):
		run(monkeypatch, capsys, "--run", RUN, "--quality-config", str(config))
	assert database.log == ["rollback", "close"]
	assert site.sent("quality_received") == []


@pytest.mark.parametrize(
	"change",
	[
		{"run": "b" * 64},
		{"site": "another.example"},
		{"format": "ucm-target-metrics-v0"},
		{"sha256": "0" * 64},
		{"metrics": None},
	],
	ids=["another run", "another site", "another format", "changed in transit", "no rows"],
)
def test_metrics_that_are_not_the_ones_asked_for_are_refused(change, site, monkeypatch, capsys, tmp_path):
	site.quality = {**answer(), **change}
	out = tmp_path / "metrics.csv"
	with pytest.raises(SystemExit, match="another run or changed in transit"):
		run(monkeypatch, capsys, "--run", RUN, "--quality-out", str(out))
	assert not out.exists()
	assert site.sent("quality_received") == []


def test_a_row_changed_in_transit_is_refused(site, monkeypatch, capsys, tmp_path):
	site.quality["metrics"][0]["target_value"] = 100
	with pytest.raises(SystemExit, match="changed in transit"):
		run(monkeypatch, capsys, "--run", RUN, "--quality-out", str(tmp_path / "m.csv"))


def test_a_refusal_by_the_site_ends_the_command(site, monkeypatch, capsys, tmp_path):
	site.status = 403
	out = tmp_path / "metrics.csv"
	with pytest.raises(SystemExit, match="quality returned HTTP 403"):
		run(monkeypatch, capsys, "--run", RUN, "--quality-out", str(out))
	assert not out.exists()


@pytest.mark.parametrize(
	"options",
	[
		["--run", RUN],
		["--quality-out", "m.csv"],
		["--run", RUN, "--quality-out", "m.csv", "--quality-table", "dq_target"],
		["--run", RUN, "--quality-out", "m.csv", "--create-quality-table"],
	],
	ids=["no destination", "no run", "a table without a database", "create without a database"],
)
def test_the_command_says_what_is_missing(options, site, monkeypatch, capsys):
	with pytest.raises(SystemExit) as stopped:
		run(monkeypatch, capsys, *options)
	assert stopped.value.code == 2
	assert site.requests == []


def test_a_destination_belongs_to_the_quality_action_only(site, monkeypatch, capsys):
	monkeypatch.setattr(
		sys, "argv", ["ucm-push", "status", "--site", SITE, "--run", RUN, "--quality-out", "m.csv"]
	)
	with pytest.raises(SystemExit) as stopped:
		push.main()
	assert stopped.value.code == 2


def test_plain_http_is_refused(site, monkeypatch, capsys, tmp_path):
	with pytest.raises(SystemExit) as stopped:
		run(
			monkeypatch,
			capsys,
			"--run",
			RUN,
			"--quality-out",
			str(tmp_path / "m.csv"),
			url="http://target.example",
		)
	assert stopped.value.code == 2
	assert site.requests == []
