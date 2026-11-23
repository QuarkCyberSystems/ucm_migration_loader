"""Push a sealed generator publication over HTTPS and restart the CLI at a chunk boundary."""

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

import frappe
from generator_snapshot import BENCH
from transport import publisher_identity, tls_site
from ucm_migration_loader.operator import approve_sealed
from ucm_migration_loader.publication_transport import receive


def deliver(site, path, run, work):
    with path.open("rb") as file:
        sha = hashlib.file_digest(file, "sha256").hexdigest()
    with (
        publisher_identity() as (publisher, token),
        tls_site(site, work) as (url, cert),
    ):
        handoff_path = work / (run + "-handoff.json")
        with handoff_path.open("w") as handle:
            subprocess.run(
                [
                    str(BENCH / "env/bin/ucm-push"),
                    "check-publication",
                    "--site",
                    site,
                    "--publication",
                    str(path),
                    "--run",
                    run,
                ],
                stdout=handle,
                check=True,
            )
        altered = json.loads(handoff_path.read_text())
        altered["snapshot"] += "-changed"
        changed_path = work / (run + "-changed-handoff.json")
        changed_path.write_text(json.dumps(altered))
        try:
            approve_sealed(str(changed_path), publisher)
        except ValueError as exc:
            assert "does not match" in str(exc)
        else:
            raise AssertionError("Changed handoff was approved")
        approval = approve_sealed(str(handoff_path), publisher)
        assert (approval["sha256"], approval["size"]) == (sha, path.stat().st_size)
        cmd = [
            str(BENCH / "env/bin/ucm-push"),
            "send-publication",
            "--site",
            site,
            "--run",
            run,
            "--publication",
            str(path),
            "--url",
            url,
        ]
        env = dict(os.environ, UCM_MIGRATION_TOKEN=token, REQUESTS_CA_BUNDLE=str(cert))

        def send(*args):
            return json.loads(
                subprocess.check_output([*cmd, *args], env=env, text=True)
            )

        status_cmd = cmd.copy()
        status_cmd[1] = "transfer-status"
        del status_cmd[
            status_cmd.index("--publication") : status_cmd.index("--publication") + 2
        ]
        initial = json.loads(subprocess.check_output(status_cmd, env=env, text=True))
        assert initial["offset"] == 0
        first = send("--max-batches", "1")
        assert 0 < first["offset"] < first["size"]
        try:
            receive(run)
        except ValueError as exc:
            assert "incomplete" in str(exc)
        else:
            raise AssertionError("Partial upload was accepted")
        complete = send()
        assert complete["offset"] == complete["size"] and send() == complete
        receipt = receive(run)
        frappe.db.commit()
        return {
            "changed_handoff_refused": True,
            "initial_bytes": first["offset"],
            "completed_bytes": complete["size"],
            "replay": True,
            "received": receipt,
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("site", "publication", "run", "evidence"):
        parser.add_argument("--" + arg, required=True)
    args = parser.parse_args()
    path, out = Path(args.publication).resolve(), Path(args.evidence).resolve()
    assert not out.exists()
    os.chdir(BENCH / "sites")
    frappe.init(site=args.site)
    frappe.connect()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            result = deliver(args.site, path, args.run, Path(tmp))
        with out.open("x") as report:
            json.dump(result, report, indent=2)
    finally:
        frappe.destroy()
