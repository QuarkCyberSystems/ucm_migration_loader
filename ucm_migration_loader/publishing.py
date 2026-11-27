"""The publishing operator's short way through `ucm-push`: what never changes on a host is kept in a settings
file, `seal` does the three steps of a publication in one, and the commands that follow find their run in the
publication's folder.

	ucm-push init                 write the settings file and the two connection files, to fill in
	ucm-push seal DEMO01          seal the delegation with that code into a new folder, with its hand-off
	ucm-push send DEMO01-2026-11-27     send the sealed file once the site operator has approved it
	ucm-push status               from inside the folder: where the run stands on the site
	ucm-push quality              collect the run's metrics

The settings file is TOML: `--config PATH`, or the path in UCM_PUSH_CONFIG, or `ucm-push.toml` in the working
folder, or `~/.config/ucm-push/config.toml`. Passwords and tokens are not kept in it: it names the private files
that hold them.

	site = "ucm.example.org"                                    # the receiving site, as its bench names it
	url = "https://ucm.example.org"                             # its HTTPS address
	source_config = "/srv/migration/credentials/source.json"    # read-only database connection (PyMySQL)
	private_files = "/srv/publication/private/files"            # the pictures of the published persons
	publications = "/srv/migration/publications"                # a folder per snapshot is made here
	token_file = "/srv/migration/credentials/ucm-token"         # key:secret of the publishing account
	quality_config = "/etc/ucm-migration/quality-db.json"       # optional: the data quality database

A person at a terminal reads sentences; a script reads JSON. The output is JSON wherever it is not a terminal,
and `--json` or `--text` choose it outright. The detailed commands (`prepare`, `check-publication`,
`send-publication` …) take every value as an option, as before.

This module runs on the publishing host: it imports nothing of Frappe.
"""

import argparse
import hashlib
import json
import os
import sys
import tomllib
from datetime import date
from pathlib import Path

SETTINGS = "ucm-push.toml"
HANDOFF, SEALED = "handoff.json", "delegation.sqlite"
ALIASES = {"send": "send-publication", "sent": "transfer-status"}
# the commands that work on one run of one sealed publication
ON_A_RUN = ("send-publication", "transfer-status", "status", "report", "quality", "locate-item")
STATE = {
	"Publication staged": "Staged: checked, nothing written yet",
	"Publication applying": "Loading",
	"Publication paused": "Paused",
	"Publication ready": "Verified, waiting for the go-live decision",
	"Publication cleaning": "Being discarded",
	"Activated": "Activated: the delegation is open to its users",
	"Discarded": "Discarded",
	"Rejected": "Refused by the site",
}


# The files a publishing host keeps, as `ucm-push init` writes them and as the repository's samples/ folder shows
# them. {folder} is where they are written; the passwords are placeholders to replace.
SETTINGS_SAMPLE = """\
# Settings of ucm-push on this publishing host: what is the same for every delegation.
# Passwords and tokens are not written here: this file names the private files that hold them.

# The receiving site, as its bench names it, and its HTTPS address
site = "ucm.example.org"
url = "https://ucm.example.org"

# The read-only connection to the publishing database: see source.json
source_config = "{folder}/source.json"

# The private files of the publishing site, for the pictures of the published persons.
# Leave this line out when the publication has no pictures.
private_files = "/srv/publication/private/files"

# One folder per snapshot is made here, with the sealed file and its hand-off
publications = "{folder}/publications"

# The API key and secret of your publishing account on the site, as key:secret on one line.
# The file must be readable by your account only: chmod 600
token_file = "{folder}/ucm-token"

# Optional: the data quality database the run's metrics are written to (see quality-db.json), and its table
# quality_config = "{folder}/quality-db.json"
# quality_table = "ucm_target_metric"

# Optional: the certificate of a private authority that signed the site's certificate
# ca_bundle = "{folder}/site-ca.pem"

# Optional: records per batch and bytes per batch (the defaults are shown)
# batch_size = 1000
# batch_bytes = 4194304
"""
SOURCE_SAMPLE = {
	"host": "publisher-db.example.org",
	"port": 3306,
	"user": "migration_reader",
	"password": "<password of the read-only account>",
	"database": "ucm_publication",
}
QUALITY_SAMPLE = {
	"host": "quality-db.example.org",
	"port": 3306,
	"user": "migration_metrics",
	"password": "<password of the metrics account>",
	"database": "data_quality",
}


def samples(folder: str) -> dict[str, str]:
	"""The three files of a publishing host, by name, for a host that keeps them in `folder`."""
	return {
		SETTINGS: SETTINGS_SAMPLE.format(folder=folder),
		"source.json": json.dumps(SOURCE_SAMPLE, indent="\t") + "\n",
		"quality-db.json": json.dumps(QUALITY_SAMPLE, indent="\t") + "\n",
	}


class Stop(SystemExit):
	"""The command cannot go on, and says why in a sentence."""


def settings(explicit: str | None = None, start: Path | None = None) -> tuple[dict, Path | None]:
	"""The host's settings, and the file they were read from: the file named, or the nearest `ucm-push.toml` from
	the working folder upwards, or the account's own. Without a file there are none: every value then comes
	from the command's options."""
	named = explicit or os.environ.get("UCM_PUSH_CONFIG")
	if named:
		path = Path(named).expanduser()
		if not path.is_file():
			raise Stop(f"Settings file not found: {path}")
		return tomllib.loads(path.read_text()), path
	folder = (start or Path.cwd()).resolve()
	near = [above / SETTINGS for above in (folder, *list(folder.parents)[:3])]
	for path in (*near, Path.home() / ".config" / "ucm-push" / "config.toml"):
		if path.is_file():
			return tomllib.loads(path.read_text()), path
	return {}, None


def credentials(conf: dict) -> None:
	"""Give the commands the publishing account's token and the site's certificate authority, from the private
	files the settings name. What the environment already holds stays."""
	if conf.get("token_file") and not os.environ.get("UCM_MIGRATION_TOKEN"):
		path = Path(conf["token_file"]).expanduser()
		if not path.is_file():
			raise Stop(f"Token file not found: {path}")
		if path.stat().st_mode & 0o077:
			raise Stop(f"{path} can be read by other accounts of this host. Run: chmod 600 {path}")
		os.environ["UCM_MIGRATION_TOKEN"] = path.read_text().strip()
	if conf.get("ca_bundle"):
		os.environ.setdefault("REQUESTS_CA_BUNDLE", str(Path(conf["ca_bundle"]).expanduser()))


def folder_of(target: str | None, conf: dict) -> Path | None:
	"""The folder of a publication: the one named (a path, or a snapshot under the settings' publications
	folder), or the working folder where it holds a hand-off."""
	if target:
		for candidate in (
			Path(target).expanduser(),
			Path(conf.get("publications", ".")).expanduser() / target,
		):
			if (candidate / HANDOFF).is_file():
				return candidate
		raise Stop(f"No sealed publication found for {target}: it has no {HANDOFF}")
	return Path.cwd() if (Path.cwd() / HANDOFF).is_file() else None


def _has(argv: list[str], option: str) -> bool:
	return any(arg == option or arg.startswith(option + "=") for arg in argv)


def _own_options(argv: list[str], terminal: bool) -> tuple[list[str], bool, str | None]:
	"""Take this module's options out of a command: how to answer, and the settings file named."""
	argv, readable, named = list(argv), terminal, None
	for flag, value in (("--json", False), ("--text", True)):
		if flag in argv:
			argv.remove(flag)
			readable = value
	if "--config" in argv:
		at = argv.index("--config")
		if at + 1 >= len(argv):
			raise Stop("--config takes the path of a settings file")
		named = argv[at + 1]
		del argv[at : at + 2]
	return argv, readable, named


def _on_a_run(argv: list[str], action: str, folder: Path | None, conf: dict, readable: bool) -> list[str]:
	"""Add to a command on one run what its publication's folder and the settings know."""
	if folder and not _has(argv, "--run"):
		argv += ["--run", json.loads((folder / HANDOFF).read_text())["run"]]
	if folder and action in ("send-publication", "locate-item") and not _has(argv, "--publication"):
		argv += ["--publication", str(folder / SEALED)]
	if action == "send-publication" and readable and not _has(argv, "--progress"):
		argv.append("--progress")
	if action == "quality" and not (_has(argv, "--quality-out") or _has(argv, "--quality-config")):
		if conf.get("quality_config"):
			argv += ["--quality-config", str(Path(conf["quality_config"]).expanduser())]
			if conf.get("quality_table"):
				argv += ["--quality-table", str(conf["quality_table"])]
		if folder:
			argv += ["--quality-out", str(folder / "metrics.csv")]
	return argv


def fill(argv: list[str], terminal: bool) -> tuple[list[str], bool]:
	"""Complete a command with what the settings and the publication's folder know, and decide how its result is
	shown. Returns the arguments for the detailed command, and whether to answer in sentences."""
	argv, readable, named = _own_options(argv, terminal)
	if not argv or argv[0].startswith("-"):
		return argv, readable
	action = argv[0] = ALIASES.get(argv[0], argv[0])
	target = argv.pop(1) if len(argv) > 1 and not argv[1].startswith("-") else None
	conf, _path = settings(named)
	credentials(conf)
	for option in ("site", "url"):
		if conf.get(option) and not _has(argv, f"--{option}"):
			argv += [f"--{option}", str(conf[option])]
	if action in ON_A_RUN:
		return _on_a_run(argv, action, folder_of(target, conf), conf, readable), readable
	if target:
		raise Stop(f"{action} takes its values as options: {target} was not expected")
	return argv, readable


# ---------------------------------------------------------------------------------------------------------------
# what a person reads


def _megabytes(size: int) -> str:
	return f"{size / 1_000_000:,.1f} MB"


def _short(run: str) -> str:
	return run[:12] + "…"


def sentences(action: str, result: dict) -> str | None:
	"""A command's result in sentences, for the commands an operator runs by hand. Nothing for the others, whose
	result is shown as JSON."""
	if action == "transfer-status":
		share = result["offset"] / result["size"] if result["size"] else 0
		done = "the whole file has arrived" if result["offset"] >= result["size"] else "not all of it yet"
		return (
			f"Run {_short(result['run'])} is approved for sending: "
			f"{_megabytes(result['offset'])} of {_megabytes(result['size'])} sent ({share:.0%}), {done}."
		)
	if action == "send-publication":
		return (
			f"Sent: {_megabytes(result['size'])}, all of it. "
			f"Run {_short(result['run'])} can now be staged by the site operator."
		)
	if action == "status":
		state = STATE.get(result["state"], result["state"])
		if result["state"] == "Publication applying":
			state += f": {result['applied']:,} of {result['received']:,} batches written"
			state += f", {result['reconciled_batches']:,} verified"
		lines = [f"Run {_short(result['run'])}: {state}."]
		if result.get("finding"):
			lines.append(f"Finding: {result['finding']}")
		return "\n".join(lines)
	if action == "quality":
		failing = "none failing" if not result["failed"] else f"{result['failed']:,} failing"
		return (
			f"{result['metrics']:,} metrics of run {_short(result['run'])} stored in {result['destination']}: "
			f"{failing}. The run has recorded the hand-over."
		)
	return None


def show(action: str, result, readable: bool) -> None:
	text = sentences(action, result) if readable and isinstance(result, dict) else None
	print(text if text is not None else json.dumps(result))


# ---------------------------------------------------------------------------------------------------------------
# seal


def find(connection, delegation: str) -> tuple[str, str, str]:
	"""The published delegation with this code, or this identifier: (identifier, code, name)."""
	with connection.cursor() as cursor:
		cursor.execute(
			"select name, context_code, context_name from tabContext where context_code = %s or name = %s",
			(delegation, delegation),
		)
		found = cursor.fetchall()
	if not found:
		raise Stop(f"The publishing database holds no delegation with the code or identifier {delegation}")
	if len(found) > 1:
		raise Stop(f"{delegation} names {len(found)} delegations: give the identifier of one")
	identifier, code, name = found[0]
	return str(identifier), code, name or code


def new_folder(publications: Path, snapshot: str | None, code: str, today: date) -> tuple[str, Path]:
	"""A snapshot name that was not used yet, and its new, private folder."""
	publications.mkdir(parents=True, exist_ok=True)
	if snapshot:
		names = [snapshot]
	else:
		base = f"{code}-{today.isoformat()}"
		names = [base, *(f"{base}-{number}" for number in range(2, 100))]
	for name in names:
		try:
			(publications / name).mkdir(mode=0o700)
		except FileExistsError:
			continue
		return name, publications / name
	raise Stop(
		f"Snapshot {names[0]} was used before: a sealed publication is never made twice under one name"
	)


def seal(conf: dict, delegation: str, snapshot: str | None = None, today: date | None = None) -> dict:
	"""Seal one delegation into a new folder: what it holds, the sealed file, and the hand-off for the site
	operator. Returns the hand-off, with the folder and the delegation's code and name."""
	import pymysql

	from .frozen import freeze, verify_publication
	from .source_inventory import inventory

	missing = [key for key in ("site", "source_config", "publications") if not conf.get(key)]
	if missing:
		raise Stop("The settings file lacks: " + ", ".join(missing))
	private = Path(conf["private_files"]).expanduser() if conf.get("private_files") else None
	with pymysql.connect(**json.loads(Path(conf["source_config"]).expanduser().read_text())) as source:
		with source.cursor() as cursor:
			# one view of the publishing database for the inventory and the sealing
			cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
			cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
		identifier, code, name = find(source, delegation)
		snapshot, folder = new_folder(
			Path(conf["publications"]).expanduser(), snapshot, code, today or date.today()
		)
		held = inventory(source, identifier)
		(folder / "inventory.json").write_text(json.dumps({"snapshot": snapshot, **held}, indent=1) + "\n")
		try:
			published = freeze(
				source,
				folder / SEALED,
				private,
				conf["site"],
				snapshot,
				identifier,
				int(conf.get("batch_size", 1000)),
				int(conf.get("batch_bytes", 4 * 1024 * 1024)),
				folder / "findings.jsonl",
				ucm_version=conf.get("ucm_version"),
			)
		except ValueError as refused:
			raise Stop(f"{code} was not sealed: {refused}. See {folder} for what was found.") from None
	manifest = verify_publication(folder / SEALED, published["run"])
	with (folder / SEALED).open("rb") as file:
		fingerprint = hashlib.file_digest(file, "sha256").hexdigest()
		size = os.fstat(file.fileno()).st_size
	handoff = {
		"run": published["run"],
		"verified": True,
		"file_sha256": fingerprint,
		"file_bytes": size,
		**manifest,
	}
	(folder / HANDOFF).write_text(json.dumps(handoff, indent=1) + "\n")
	return {**handoff, "folder": str(folder), "delegation_code": code, "delegation_name": name}


def sealed_sentences(result: dict) -> str:
	counts = {table: rows for table, rows in result["counts"].items() if rows}
	tables = " · ".join(f"{table} {rows:,}" for table, rows in sorted(counts.items(), key=lambda kv: -kv[1]))
	return "\n".join(
		(
			f"Sealed {result['delegation_code']} ({result['delegation_name']}) as snapshot {result['snapshot']}",
			f"  Folder       {result['folder']}",
			f"  Rows         {sum(counts.values()):,} in {result['batches']:,} batches",
			f"               {tables}",
			f"  File         {SEALED}, {_megabytes(result['file_bytes'])}",
			f"  Fingerprint  {result['file_sha256']}",
			f"  Run          {result['run']}",
			"",
			f"Next: give {HANDOFF} from that folder to the site operator. After their approval:",
			f"  ucm-push send {result['snapshot']}",
		)
	)


def seal_command(argv: list[str], terminal: bool) -> None:
	parser = argparse.ArgumentParser(
		prog="ucm-push seal", description="Seal one delegation, with its hand-off, into a new folder."
	)
	parser.add_argument("delegation", help="The delegation's code, or its identifier")
	parser.add_argument("--snapshot", help="The snapshot's name (default: the code and today's date)")
	parser.add_argument("--config", help="The settings file")
	parser.add_argument("--json", action="store_true", help="Answer in JSON")
	parser.add_argument("--text", action="store_true", help="Answer in sentences")
	args = parser.parse_args(argv)
	conf, path = settings(args.config)
	if path is None:
		raise Stop(f"seal reads the host's settings: ucm-push init writes {SETTINGS} to fill in")
	result = seal(conf, args.delegation, args.snapshot)
	readable = args.text or (terminal and not args.json)
	print(sealed_sentences(result) if readable else json.dumps(result))


def init_command(argv: list[str], terminal: bool) -> None:
	"""Write the host's files to fill in: the settings and the two database connections. A file that is there
	is left as it is."""
	parser = argparse.ArgumentParser(
		prog="ucm-push init", description="Write the settings file and the two connection files, to fill in."
	)
	parser.add_argument("folder", nargs="?", default=".", help="Where to write them (default: here)")
	parser.add_argument("--json", action="store_true", help="Answer in JSON")
	parser.add_argument("--text", action="store_true", help="Answer in sentences")
	args = parser.parse_args(argv)
	folder = Path(args.folder).expanduser().resolve()
	folder.mkdir(mode=0o700, parents=True, exist_ok=True)
	written, kept = [], []
	for name, text in samples(str(folder)).items():
		path = folder / name
		if path.exists():
			kept.append(name)
			continue
		# a connection file will hold a password: private from the moment it exists
		path.touch(mode=0o600)
		path.write_text(text)
		written.append(name)
	if not (args.text or (terminal and not args.json)):
		print(json.dumps({"folder": str(folder), "written": written, "kept": kept}))
		return
	lines = [f"In {folder}:"]
	lines += [f"  wrote {name}" for name in written]
	lines += [f"  kept  {name} (it was there already)" for name in kept]
	lines += [
		"",
		f"Fill them in: the site and its address in {SETTINGS}, the read-only account in source.json,",
		"and the metrics account in quality-db.json if you write metrics to a database.",
		f"Put your publishing account's key:secret in {folder / 'ucm-token'} and run: chmod 600 {folder / 'ucm-token'}",
		"Then, from that folder: ucm-push seal CODE",
	]
	print("\n".join(lines))


def main(argv: list[str] | None = None) -> None:
	"""`ucm-push`: the short commands here, and the detailed ones of `push`."""
	from . import push

	argv = sys.argv[1:] if argv is None else argv
	terminal = sys.stdout.isatty()
	if argv[:1] == ["--help-settings"]:
		print(__doc__)
		return
	if argv[:1] == ["init"]:
		init_command(argv[1:], terminal)
		return
	if argv[:1] == ["seal"]:
		seal_command(argv[1:], terminal)
		return
	argv, readable = fill(argv, terminal)
	push.run(argv, readable)
