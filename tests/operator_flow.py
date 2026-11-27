"""The short way through a migration, with the commands the two operators type.

	env/bin/python apps/ucm_migration_loader/tests/operator_flow.py --site <site> --delegation <code> \
		--source-config <source.json> [--private-files <folder>] [--discard]

Run from the bench. The publishing operator's side is `ucm-push` with a settings file written for this run:
`seal`, `sent`, `send`, `status` and `quality`, each as a process of its own, answering in sentences. The site
operator's side is `bench ucm-migration approve` and `stage`. The site is reached over a loopback TLS listener,
with a publishing account made for this run. Between staging and the metrics the delegation is loaded by the
set-based route in this process. The run is left ready and hidden, or discarded with `--discard`.

The delegation's context must exist on the site, locked, hidden and empty. Synthetic data only: the source is a
site filled by the data generator.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import frappe

BENCH = Path(__file__).resolve().parents[3]


def expect(condition: bool, message: str) -> None:
	if not condition:
		raise AssertionError(message)


def typed(command: list[str], env: dict, cwd: Path, code: int = 0) -> str:
	"""Run one command as an operator types it, and return what it answers."""
	done = subprocess.run(
		command, env=env, cwd=cwd, check=False, capture_output=True, text=True, timeout=1800
	)
	if done.returncode != code:
		raise RuntimeError(f"{' '.join(command[:4])} ended with {done.returncode}: {done.stderr[-600:]}")
	# other apps of the bench may write their own lines beside the command's answer
	return "\n".join(line for line in done.stdout.splitlines() if not line.startswith("[")).strip()


def flow(args) -> dict:
	from set_based_queue import connect, record
	from transport import publisher_identity, tls_site

	from ucm_migration_loader.ucm_migration_loader import set_based

	connect(args.site)
	said = {}
	with (
		tempfile.TemporaryDirectory(prefix="ucm-operators-") as folder,
		tls_site(args.site, Path(folder)) as (url, cert),
		publisher_identity() as (publisher, token),
	):
		work = Path(folder)
		(work / "token").write_text(token + "\n")
		(work / "token").chmod(0o600)
		lines = {
			"site": args.site,
			"url": url,
			"source_config": str(args.source_config.resolve()),
			"publications": str(work / "publications"),
			"token_file": str(work / "token"),
			"ca_bundle": str(cert),
		}
		if args.private_files:
			lines["private_files"] = str(args.private_files.resolve())
		(work / "ucm-push.toml").write_text("".join(f'{key} = "{value}"\n' for key, value in lines.items()))
		env = {**os.environ, "NO_PROXY": "localhost", "no_proxy": "localhost"}
		env.pop("UCM_MIGRATION_TOKEN", None)
		push = str(BENCH / "env/bin/ucm-push")
		bench = ["bench", "--site", args.site, "ucm-migration"]

		# the publishing operator seals; the settings file is found from the working folder
		said["seal"] = typed([push, "seal", args.delegation, "--text"], env, work)
		(sealed,) = (work / "publications").iterdir()
		snapshot, handoff = sealed.name, sealed / "handoff.json"
		run = json.loads(handoff.read_text())["run"]
		expect(
			f"ucm-push send {snapshot}" in said["seal"], f"seal did not say what comes next: {said['seal']}"
		)
		expect(sealed.stat().st_mode & 0o777 == 0o700, "the publication's folder is not private")

		# the site operator approves the hand-off for the publishing account
		said["approve"] = typed([*bench, "approve", str(handoff), "--publisher", publisher], env, BENCH)
		expect("Approved run" in said["approve"], said["approve"])
		early = typed([*bench, "stage", str(handoff)], env, BENCH, code=1)
		expect("has not arrived completely" in early, f"an empty transfer was staged: {early}")

		# the publishing operator sends, by the snapshot's name
		said["before sending"] = typed([push, "sent", snapshot, "--text"], env, work)
		said["send"] = typed([push, "send", snapshot, "--text"], env, work)
		expect(said["send"].startswith("Sent:"), said["send"])

		# the site operator stages
		said["stage"] = typed([*bench, "stage", str(handoff)], env, BENCH)
		expect("Staged as MIG-" in said["stage"], said["stage"])
		said["status, staged"] = typed([push, "status", "--text"], env, sealed)
		expect("Staged: checked, nothing written yet" in said["status, staged"], said["status, staged"])

		# the migration team loads it (here by the set-based steps, from the bench)
		set_based.apply(run)
		set_based.derive(run)
		report = set_based.verify(run)
		expect(report["state"] == "Publication ready" and not report["findings"], f"the load: {report}")

		# the publishing operator reads the result and collects the metrics, from inside the folder
		said["status, verified"] = typed([push, "status", "--text"], env, sealed)
		expect(
			"Verified, waiting for the go-live decision" in said["status, verified"], said["status, verified"]
		)
		said["quality"] = typed([push, "quality", "--text"], env, sealed)
		expect("stored in file metrics.csv" in said["quality"], said["quality"])
		expect((sealed / "metrics.csv").stat().st_size > 0, "no metrics file in the publication's folder")
		as_json = json.loads(typed([push, "status"], env, sealed))
		expect(as_json["state"] == "Publication ready", f"a script did not get JSON: {as_json}")

		current = record(run)
		handed = json.loads(current.quality_json or "{}")
		expect(handed.get("by") == publisher, f"the hand-over was not recorded for the sender: {handed}")
		out = {
			"run": run,
			"display_id": current.display_id,
			"snapshot": snapshot,
			"rows": current.document_count,
			"state": current.state,
		}
		if args.discard:
			set_based.discard(run)
			# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: the end of a rehearsal run from the bench
			frappe.db.commit()
			out["state"] = record(run).state
		else:
			# the publishing account goes with this run: the run keeps no account that no longer exists
			frappe.db.set_value("UCM Migration Run", run, "publishing_user", None, update_modified=False)
			# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: the end of a rehearsal run from the bench
			frappe.db.commit()
	return {**out, "said": said}


def main() -> None:
	parser = argparse.ArgumentParser(
		description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
	)
	parser.add_argument("--site", required=True)
	parser.add_argument(
		"--delegation", required=True, help="The delegation's code in the publishing database"
	)
	parser.add_argument(
		"--source-config", type=Path, required=True, help="PyMySQL connection JSON of the source"
	)
	parser.add_argument("--private-files", type=Path, help="The source's private files, for the pictures")
	parser.add_argument("--discard", action="store_true", help="Discard the run at the end")
	parser.add_argument("--evidence", type=Path, help="Write the result here as well")
	args = parser.parse_args()
	evidence = args.evidence.resolve() if args.evidence else None
	args.source_config = args.source_config.resolve()
	args.private_files = args.private_files.resolve() if args.private_files else None
	sys.path.insert(0, str(Path(__file__).resolve().parent))
	try:
		result = flow(args)
	finally:
		frappe.destroy()
	text = json.dumps(result, indent=1, default=str)
	print(text)
	if evidence:
		evidence.write_text(text + "\n")


if __name__ == "__main__":
	main()
