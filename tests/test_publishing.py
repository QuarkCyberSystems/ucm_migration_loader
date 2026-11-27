"""The publishing operator's short commands, without a site or a database: the settings file, what a command is
completed with, what an operator reads, and `seal` with the publishing database and the sealing played back."""

import json
import os
import sys
from datetime import date
from pathlib import Path

import pytest

from ucm_migration_loader import publishing, push

RUN = "a" * 64
CONTEXT = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def host(tmp_path, monkeypatch):
	"""A publishing host: a settings file, a publications folder, and one sealed publication in it."""
	monkeypatch.chdir(tmp_path)
	monkeypatch.delenv("UCM_PUSH_CONFIG", raising=False)
	monkeypatch.delenv("UCM_MIGRATION_TOKEN", raising=False)
	monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
	publications = tmp_path / "publications"
	(tmp_path / "ucm-push.toml").write_text(
		f'site = "target.example"\nurl = "https://target.example"\n'
		f'source_config = "{tmp_path / "source.json"}"\npublications = "{publications}"\n'
	)
	(tmp_path / "source.json").write_text("{}")
	sealed = publications / "DEMO01-2026-01-02"
	sealed.mkdir(parents=True)
	(sealed / "handoff.json").write_text(json.dumps({"run": RUN}))
	(sealed / "delegation.sqlite").write_bytes(b"sealed")
	return tmp_path


def option(argv: list[str], name: str) -> str:
	return argv[argv.index(name) + 1]


# --- the settings file --------------------------------------------------------------------------------------


def test_the_nearest_settings_file_is_read_from_a_publications_folder(host, monkeypatch):
	monkeypatch.chdir(host / "publications" / "DEMO01-2026-01-02")
	conf, path = publishing.settings()
	assert path == host / "ucm-push.toml" and conf["site"] == "target.example"


def test_a_named_settings_file_must_exist(host, monkeypatch):
	with pytest.raises(publishing.Stop, match="Settings file not found"):
		publishing.settings(str(host / "other.toml"))
	monkeypatch.setenv("UCM_PUSH_CONFIG", str(host / "ucm-push.toml"))
	monkeypatch.chdir(host / "publications")
	assert publishing.settings()[1] == host / "ucm-push.toml"


def test_without_a_settings_file_there_are_no_settings(tmp_path, monkeypatch):
	monkeypatch.chdir(tmp_path)
	monkeypatch.delenv("UCM_PUSH_CONFIG", raising=False)
	monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
	assert publishing.settings() == ({}, None)


def test_the_token_comes_from_a_private_file(host, monkeypatch):
	token = host / "token"
	token.write_text("key:secret\n")
	token.chmod(0o640)
	with pytest.raises(publishing.Stop, match="chmod 600"):
		publishing.credentials({"token_file": str(token)})
	token.chmod(0o600)
	publishing.credentials({"token_file": str(token), "ca_bundle": str(host / "ca.pem")})
	assert os.environ["UCM_MIGRATION_TOKEN"] == "key:secret"
	assert os.environ["REQUESTS_CA_BUNDLE"] == str(host / "ca.pem")
	monkeypatch.setenv("UCM_MIGRATION_TOKEN", "from:environment")
	publishing.credentials({"token_file": str(token)})
	assert os.environ["UCM_MIGRATION_TOKEN"] == "from:environment"
	monkeypatch.delenv("REQUESTS_CA_BUNDLE")


# --- what a command is completed with ------------------------------------------------------------------------


def test_a_snapshot_name_gives_the_site_the_run_and_the_file(host):
	argv, readable = publishing.fill(["send", "DEMO01-2026-01-02"], terminal=False)
	assert argv[0] == "send-publication" and not readable
	assert (option(argv, "--site"), option(argv, "--url")) == ("target.example", "https://target.example")
	assert option(argv, "--run") == RUN
	assert option(argv, "--publication") == str(host / "publications/DEMO01-2026-01-02/delegation.sqlite")
	assert "--progress" not in argv


def test_inside_the_folder_no_name_is_needed(host, monkeypatch):
	monkeypatch.chdir(host / "publications" / "DEMO01-2026-01-02")
	argv, readable = publishing.fill(["status"], terminal=True)
	assert readable and option(argv, "--run") == RUN and "--publication" not in argv


def test_an_option_given_is_kept(host):
	argv, _ = publishing.fill(["status", "DEMO01-2026-01-02", "--run", "b" * 64, "--site", "other"], False)
	assert (option(argv, "--run"), option(argv, "--site")) == ("b" * 64, "other")
	assert argv.count("--run") == argv.count("--site") == 1


def test_the_answer_is_json_unless_a_person_reads_it(host):
	assert publishing.fill(["status", "DEMO01-2026-01-02"], terminal=True)[1] is True
	assert publishing.fill(["status", "DEMO01-2026-01-02", "--json"], terminal=True)[1] is False
	argv, readable = publishing.fill(["status", "DEMO01-2026-01-02", "--text"], terminal=False)
	assert readable and "--text" not in argv


def test_a_person_sending_sees_the_progress(host):
	assert "--progress" in publishing.fill(["send", "DEMO01-2026-01-02"], terminal=True)[0]


def test_quality_goes_to_the_folder_and_to_the_database_the_settings_name(host):
	argv, _ = publishing.fill(["quality", "DEMO01-2026-01-02"], terminal=False)
	assert option(argv, "--quality-out") == str(host / "publications/DEMO01-2026-01-02/metrics.csv")
	assert "--quality-config" not in argv
	with (host / "ucm-push.toml").open("a") as file:
		file.write('quality_config = "/etc/quality.json"\nquality_table = "dq_target"\n')
	argv, _ = publishing.fill(["quality", "DEMO01-2026-01-02"], terminal=False)
	assert (option(argv, "--quality-config"), option(argv, "--quality-table")) == (
		"/etc/quality.json",
		"dq_target",
	)
	argv, _ = publishing.fill(["quality", "DEMO01-2026-01-02", "--quality-out", "mine.csv"], terminal=False)
	assert option(argv, "--quality-out") == "mine.csv" and "--quality-config" not in argv


def test_an_unknown_publication_is_said_so(host):
	with pytest.raises(publishing.Stop, match="No sealed publication found for NOPE"):
		publishing.fill(["status", "NOPE"], terminal=False)
	with pytest.raises(publishing.Stop, match="was not expected"):
		publishing.fill(["prepare", "DEMO01-2026-01-02"], terminal=False)


def test_a_detailed_command_without_settings_is_left_as_it_is(tmp_path, monkeypatch):
	monkeypatch.chdir(tmp_path)
	monkeypatch.delenv("UCM_PUSH_CONFIG", raising=False)
	monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
	given = ["status", "--site", "s", "--url", "https://s", "--run", RUN]
	assert publishing.fill(given, terminal=False) == (given, False)
	assert publishing.fill(["--help"], terminal=True) == (["--help"], True)


# --- what an operator reads ----------------------------------------------------------------------------------


def test_a_status_is_told_in_the_teams_words():
	loading = {
		"run": RUN,
		"state": "Publication applying",
		"applied": 53,
		"received": 223,
		"reconciled_batches": 0,
		"finding": None,
	}
	assert publishing.sentences("status", loading) == (
		"Run aaaaaaaaaaaa…: Loading: 53 of 223 batches written, 0 verified."
	)
	paused = {
		**loading,
		"state": "Publication paused",
		"finding": "apply batch 5, item 19 (Person): ValueError",
	}
	assert publishing.sentences("status", paused).splitlines() == [
		"Run aaaaaaaaaaaa…: Paused.",
		"Finding: apply batch 5, item 19 (Person): ValueError",
	]
	approved = publishing.sentences("status", {**loading, "state": "Awaiting publication"})
	assert approved == "Run aaaaaaaaaaaa…: Approved: the site waits for the sealed file."
	ready = publishing.sentences("status", {**loading, "state": "Publication ready"})
	assert ready == "Run aaaaaaaaaaaa…: Verified, waiting for the go-live decision."


def test_a_transfer_and_a_hand_over_are_told_in_a_sentence():
	receipt = {"run": RUN, "site": "s", "size": 295_000_000, "sha256": "f" * 64, "offset": 118_000_000}
	assert "118.0 MB of 295.0 MB sent (40%), not all of it yet" in publishing.sentences(
		"transfer-status", receipt
	)
	sent = publishing.sentences("send-publication", {**receipt, "offset": 295_000_000})
	assert (
		sent
		== "Sent: 295.0 MB, all of it. The site stages run aaaaaaaaaaaa… by itself: ucm-push status shows when."
	)
	stored = {"run": RUN, "metrics": 69, "failed": 0, "destination": "file metrics.csv, table dq_target"}
	assert publishing.sentences("quality", stored) == (
		"69 metrics of run aaaaaaaaaaaa… stored in file metrics.csv, table dq_target: none failing. "
		"The run has recorded the hand-over."
	)
	assert "11 failing" in publishing.sentences("quality", {**stored, "failed": 11})
	assert publishing.sentences("report", {"run": RUN}) is None


def test_a_script_gets_json(capsys):
	publishing.show("status", {"run": RUN, "state": "Activated"}, readable=False)
	assert json.loads(capsys.readouterr().out) == {"run": RUN, "state": "Activated"}
	publishing.show("report", {"run": RUN}, readable=True)
	assert json.loads(capsys.readouterr().out) == {"run": RUN}


# --- seal ------------------------------------------------------------------------------------------------------


class Source:
	"""The publishing database, with the delegations it answers for."""

	def __init__(self, found):
		self.found, self.asked = found, []

	def __enter__(self):
		return self

	def __exit__(self, *_exc):
		pass

	def cursor(self):
		return self

	def execute(self, sql, values=None):
		self.asked.append(sql.split()[0].lower())

	def fetchall(self):
		return self.found


@pytest.fixture
def sealing(host, monkeypatch):
	"""Play back the publishing database and the sealing: one delegation, DEMO01."""
	import pymysql

	from ucm_migration_loader import frozen, source_inventory

	calls = {}
	source = Source([(CONTEXT, "DEMO01", "Synthetic delegation")])
	monkeypatch.setattr(pymysql, "connect", lambda **config: source)
	monkeypatch.setattr(
		source_inventory,
		"inventory",
		lambda src, context: {"context": context, "unsupported_business_rows": {}},
	)

	def freeze(src, output, root, site, snapshot, context, size, byte_limit, findings, *, ucm_version=None):
		calls["freeze"] = {"site": site, "snapshot": snapshot, "context": context, "size": size, "root": root}
		output.write_bytes(b"sealed bytes")
		return {"run": RUN}

	manifest = {
		"site": "target.example",
		"snapshot": "?",
		"counts": {"Person": 1000, "Person Note": 1000, "ToDo": 0},
		"batches": 2,
	}
	monkeypatch.setattr(frozen, "freeze", freeze)
	monkeypatch.setattr(
		frozen, "verify_publication", lambda path, run: {**manifest, "snapshot": path.parent.name}
	)
	return host, calls, source


def test_seal_makes_the_folder_the_file_and_the_hand_off(sealing):
	host, calls, source = sealing
	conf, _ = publishing.settings()
	result = publishing.seal(conf, "DEMO01", today=date(2026, 3, 4))
	folder = host / "publications" / "DEMO01-2026-03-04"
	assert result["folder"] == str(folder) and folder.stat().st_mode & 0o777 == 0o700
	assert sorted(p.name for p in folder.iterdir()) == ["delegation.sqlite", "handoff.json", "inventory.json"]
	handoff = json.loads((folder / "handoff.json").read_text())
	assert (handoff["run"], handoff["verified"], handoff["file_bytes"]) == (RUN, True, len(b"sealed bytes"))
	assert len(handoff["file_sha256"]) == 64 and "folder" not in handoff
	assert handoff["delegation"] == {"code": "DEMO01", "name": "Synthetic delegation"}
	assert calls["freeze"] == {
		"site": "target.example",
		"snapshot": "DEMO01-2026-03-04",
		"context": CONTEXT,
		"size": 1000,
		"root": None,
	}
	# the inventory and the sealing read one view of the publishing database
	assert source.asked[:2] == ["set", "start"]


def test_a_second_seal_of_the_day_takes_a_new_name_and_a_given_name_is_never_reused(sealing):
	conf, _ = publishing.settings()
	first = publishing.seal(conf, CONTEXT, today=date(2026, 3, 4))
	second = publishing.seal(conf, "DEMO01", today=date(2026, 3, 4))
	assert (first["snapshot"], second["snapshot"]) == ("DEMO01-2026-03-04", "DEMO01-2026-03-04-2")
	assert publishing.seal(conf, "DEMO01", snapshot="wave-3")["snapshot"] == "wave-3"
	with pytest.raises(publishing.Stop, match="was used before"):
		publishing.seal(conf, "DEMO01", snapshot="wave-3")


def test_a_delegation_that_cannot_be_sealed_says_why(sealing, monkeypatch):
	host, _calls, source = sealing
	from ucm_migration_loader import frozen

	conf, _ = publishing.settings()

	def refuse(*_args, **_kwargs):
		raise ValueError("3 activities lack a required place")

	monkeypatch.setattr(frozen, "freeze", refuse)
	with pytest.raises(publishing.Stop, match="DEMO01 was not sealed: 3 activities lack a required place"):
		publishing.seal(conf, "DEMO01", today=date(2026, 3, 4))
	assert not (host / "publications" / "DEMO01-2026-03-04" / "handoff.json").exists()
	source.found = []
	with pytest.raises(publishing.Stop, match="holds no delegation with the code or identifier NOPE"):
		publishing.seal(conf, "NOPE")
	with pytest.raises(publishing.Stop, match="lacks: source_config, publications"):
		publishing.seal({"site": "s"}, "DEMO01")


def test_the_operator_reads_what_was_sealed_and_what_comes_next(sealing, monkeypatch, capsys):
	monkeypatch.setattr(sys, "argv", ["ucm-push", "seal", "DEMO01", "--text"])
	push.main()
	told = capsys.readouterr().out
	snapshot = f"DEMO01-{date.today().isoformat()}"
	assert f"Sealed DEMO01 (Synthetic delegation) as snapshot {snapshot}" in told
	assert "2,000 in 2 batches" in told and "Person 1,000 · Person Note 1,000" in told
	assert told.rstrip().endswith(f"ucm-push send {snapshot}")
	assert f"ucm-push propose {snapshot}" in told
	monkeypatch.setattr(sys, "argv", ["ucm-push", "seal", "DEMO01", "--json"])
	push.main()
	assert json.loads(capsys.readouterr().out)["delegation_code"] == "DEMO01"


def test_seal_needs_the_hosts_settings(tmp_path, monkeypatch):
	monkeypatch.chdir(tmp_path)
	monkeypatch.delenv("UCM_PUSH_CONFIG", raising=False)
	monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
	with pytest.raises(publishing.Stop, match="seal reads the host's settings"):
		publishing.main(["seal", "DEMO01"])


def test_the_settings_file_is_explained(capsys):
	publishing.main(["--help-settings"])
	assert 'source_config = "/srv/migration/credentials/source.json"' in capsys.readouterr().out


# --- proposing a hand-off ---------------------------------------------------------------------------------------


def test_the_check_code_is_the_start_of_the_files_fingerprint():
	from ucm_migration_loader.contract import check_code

	assert check_code("ae064673bb9551a3" + "0" * 48) == "AE06-4673"


def test_propose_sends_the_hand_off_of_the_folder(host):
	argv, _ = publishing.fill(["propose", "DEMO01-2026-01-02"], terminal=False)
	assert argv[0] == "propose"
	assert option(argv, "--handoff") == str(host / "publications/DEMO01-2026-01-02/handoff.json")
	assert option(argv, "--site") == "target.example"


def test_the_operator_is_told_the_check_code_to_pass_on(host, monkeypatch, capsys):
	sealed = host / "publications" / "DEMO01-2026-01-02"
	handoff = {"run": RUN, "site": "target.example", "file_sha256": "ae064673" + "0" * 56}
	(sealed / "handoff.json").write_text(json.dumps(handoff))
	sent = []

	class Session:
		def mount(self, *_args):
			pass

		def post(self, url, json, headers, timeout, allow_redirects):
			sent.append((url.rsplit(".", 1)[1], json))

			class Response:
				status_code = 200

				def json(self):
					return {"message": {"run": RUN, "display_id": "MIG-0070", "state": "Proposed"}}

			return Response()

	monkeypatch.setattr(push.requests, "Session", Session)
	monkeypatch.setenv("UCM_MIGRATION_TOKEN", "key:secret")
	publishing.main(["propose", "DEMO01-2026-01-02", "--text"])
	told = capsys.readouterr().out
	assert sent == [("propose", {"handoff": handoff})]
	assert "Proposed to the site as MIG-0070." in told and "check code yourself: AE06-4673" in told
	publishing.main(["propose", "DEMO01-2026-01-02", "--json"])
	assert json.loads(capsys.readouterr().out)["check_code"] == "AE06-4673"


def test_a_hand_off_for_another_site_is_not_proposed(host, monkeypatch):
	sealed = host / "publications" / "DEMO01-2026-01-02"
	(sealed / "handoff.json").write_text(
		json.dumps({"run": RUN, "site": "elsewhere", "file_sha256": "0" * 64})
	)
	monkeypatch.setenv("UCM_MIGRATION_TOKEN", "key:secret")
	with pytest.raises(ValueError, match="another site"):
		publishing.main(["propose", "DEMO01-2026-01-02", "--json"])


def test_a_proposal_already_on_the_site_is_said_so():
	known = {"run": RUN, "display_id": "MIG-0070", "state": "Awaiting publication", "check_code": "AE06-4673"}
	assert publishing.sentences("propose", known) == (
		"Run aaaaaaaaaaaa… is on the site already, as MIG-0070: Approved: the site waits for the sealed file."
	)
	waiting = publishing.sentences("status", {"run": RUN, "state": "Proposed", "finding": None})
	assert waiting == "Run aaaaaaaaaaaa…: Proposed: the site's migration team has not approved it yet."


# --- the host's files -----------------------------------------------------------------------------------------


def test_init_writes_the_three_files_private_and_keeps_what_is_there(tmp_path, capsys):
	folder = tmp_path / "host"
	publishing.main(["init", str(folder), "--json"])
	told = json.loads(capsys.readouterr().out)
	assert told["written"] == ["ucm-push.toml", "source.json", "quality-db.json"] and not told["kept"]
	assert folder.stat().st_mode & 0o777 == 0o700
	assert all((folder / name).stat().st_mode & 0o777 == 0o600 for name in told["written"])
	(folder / "source.json").write_text('{"host": "mine"}')
	publishing.main(["init", str(folder), "--text"])
	assert "kept  source.json (it was there already)" in capsys.readouterr().out
	assert json.loads((folder / "source.json").read_text()) == {"host": "mine"}


def test_the_files_init_writes_can_be_read_as_settings(tmp_path):
	import tomllib

	folder = tmp_path / "host"
	publishing.main(["init", str(folder), "--json"])
	conf, path = publishing.settings(str(folder / "ucm-push.toml"))
	assert path == folder / "ucm-push.toml"
	assert conf["source_config"] == str(folder / "source.json")
	assert conf["publications"] == str(folder / "publications")
	assert "quality_config" not in conf  # optional settings are shown as comments
	connection = json.loads((folder / "source.json").read_text())
	assert set(connection) == {"host", "port", "user", "password", "database"}
	assert connection["password"].startswith("<")  # a placeholder, never a password
	assert tomllib.loads((folder / "ucm-push.toml").read_text()) == conf


def test_the_repositorys_samples_are_the_files_init_writes():
	"""samples/ shows the formats to who reads the repository: it cannot drift from what the command writes."""
	shown = Path(__file__).resolve().parents[1] / "samples"
	for name, text in publishing.samples("/srv/migration").items():
		assert (shown / name).read_text() == text, name
