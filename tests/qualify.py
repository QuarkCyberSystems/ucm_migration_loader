"""Run the synthetic migration qualification suite and retain its run IDs and measurements."""

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from rehearsal_bench import BENCH

TESTS = Path(__file__).resolve().parent


def release():
    apps = {}
    for app in ("frappe", "ucm", os.environ["UCM_FIXTURE_APP"]):
        base = BENCH / "apps" / app
        revision = subprocess.check_output(
            ["git", "-C", str(base), "rev-parse", "HEAD"], text=True
        ).strip()
        dirty = subprocess.check_output(
            ["git", "-C", str(base), "status", "--porcelain"], text=True
        )
        apps[app] = {"revision": revision, "local_changes": bool(dirty.strip())}
    from ucm_migration_loader import __file__ as loader_file

    installed = Path(loader_file).parent
    code = hashlib.sha256()
    for file in sorted(
        f for f in installed.rglob("*") if f.suffix in (".py", ".js", ".json", ".txt")
    ):
        code.update(
            str(file.relative_to(installed)).encode()
            + b"\0"
            + hashlib.sha256(file.read_bytes()).digest()
        )
    apps["ucm_migration_loader"] = {"installed_files_sha256": code.hexdigest()}
    return apps


def provision(site):
    from frappe.commands.site import new_site

    if (
        not re.fullmatch(r"[a-z0-9-]+\.localhost", site)
        or (BENCH / "sites" / site).exists()
    ):
        raise ValueError("Provisioning requires an unused local rehearsal site name")
    # Invoke the same bench command in process to keep both credentials out of argv.
    previous = Path.cwd()
    try:
        os.chdir(BENCH / "sites")
        options = [
            site,
            "--admin-password",
            secrets.token_urlsafe(40),
            "--install-app",
            "ucm",
            "--install-app",
            "ucm_migration_loader",
        ]
        if password := os.environ.get("MARIADB_ROOT_PASS"):
            options.extend(["--db-root-password", password])
        new_site.main(args=options, standalone_mode=False)
    finally:
        os.chdir(previous)


def run(args, evidence, work):
    site, target = args.fresh_site, args.target
    if site == target:
        raise ValueError(
            "The fresh first-wave site must be separate from the populated target"
        )
    socket = str(args.publisher_socket.resolve(strict=True))
    baseline = work / "generator-baseline.json"
    steps = [
        (
            "generator_source",
            [
                str(TESTS / "generator_coverage.py"),
                "--source-site",
                args.source_site,
                "--label",
                args.source_label,
                "--target",
                target,
                "--evidence",
                str(baseline),
            ],
        ),
        (
            "generator_repeat_read",
            [
                str(TESTS / "generator_snapshot.py"),
                "--site",
                args.source_site,
                "--expected",
                str(baseline),
            ],
        ),
        ("contract", ["-m", "pytest", "-q", str(TESTS / "test_contract.py")]),
        (
            "generator_cases",
            [
                str(TESTS / "generator_cases.py"),
                "--source-site",
                args.source_site,
                "--target",
                site,
            ],
        ),
        (
            "generator_existing_keys",
            [
                str(TESTS / "generator_collisions.py"),
                "--source-site",
                args.source_site,
                "--target",
                target,
            ],
        ),
        (
            "first_then_second",
            [
                str(TESTS / "first_wave.py"),
                "--site",
                site,
                "--publisher-socket",
                socket,
            ],
        ),
        ("business_batch_recovery", [str(TESTS / "batch_recovery.py"), "--site", site]),
        (
            "live_caseworker",
            [str(TESTS / "live_load.py"), "--site", site, "--publisher-socket", socket],
        ),
        (
            "populated_target",
            [
                str(TESTS / "transport.py"),
                "--site",
                target,
                "--publisher-socket",
                socket,
            ],
        ),
    ]
    if args.access_source_site:
        steps.append(
            (
                "generator_consent_worker",
                [
                    str(TESTS / "consent_worker.py"),
                    "--source-site",
                    args.access_source_site,
                    "--target",
                    site,
                ],
            )
        )
        steps.insert(
            2,
            (
                "generator_names_and_consent",
                [
                    str(TESTS / "generator_consent.py"),
                    "--source-site",
                    args.access_source_site,
                    "--target",
                    site,
                ],
            ),
        )
    evidence.update(
        {
            "started_utc": datetime.now(UTC).isoformat(),
            "fresh_site": site,
            "populated_target": target,
            "apps": release(),
            "steps": {},
        }
    )
    if args.provision:
        provision(site)
    for name, command in steps:
        step = evidence["steps"][name] = {"status": "running"}
        started = monotonic()
        print(f"Running {name}", flush=True)
        result = subprocess.run(
            [sys.executable, *command],
            cwd=BENCH,
            stdout=subprocess.PIPE,
            text=True,
            check=False,
        )
        step["seconds"] = round(monotonic() - started, 2)
        if result.returncode:
            step.update({"status": "failed", "exit_code": result.returncode})
            raise RuntimeError(f"{name} exited with code {result.returncode}")
        step["result"] = (
            result.stdout.strip().splitlines()[-1]
            if name == "contract"
            else json.loads(result.stdout.strip().splitlines()[-1])
        )
        step["status"] = "passed"
        if name == "generator_source":
            step["result"] = json.loads(baseline.read_text())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fresh-site", required=True)
    parser.add_argument(
        "--access-source-site",
        help="Additional fixture publication with nonempty alternate names and consent records",
    )
    parser.add_argument("--source-site", default="ucm-generator.localhost")
    parser.add_argument("--source-label", default="migration-source-96")
    parser.add_argument(
        "--provision",
        action="store_true",
        help="Install a new synthetic UCM site before running",
    )
    parser.add_argument(
        "--target",
        required=True,
        help="Populated development site for the transport regression",
    )
    parser.add_argument("--publisher-socket", type=Path, required=True)
    parser.add_argument(
        "--evidence", type=Path, required=True, help="New JSON evidence file"
    )
    args = parser.parse_args()
    evidence = {}
    with args.evidence.open("x", encoding="utf-8") as output:
        try:
            with tempfile.TemporaryDirectory(prefix="ucm-qualification-") as work:
                run(args, evidence, Path(work))
            evidence["status"] = "passed"
        except BaseException:
            evidence["status"] = "failed"
            raise
        finally:
            evidence["finished_utc"] = datetime.now(UTC).isoformat()
            json.dump(evidence, output, indent=2)
            output.write("\n")
