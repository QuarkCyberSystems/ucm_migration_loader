"""Rehearse real CLI process restarts, a persistent publisher and token-authenticated TLS requests."""

import argparse
import getpass
import json
import os
import secrets
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from threading import Thread
from uuid import uuid4

import frappe
import pymysql
import requests
from rehearsal_bench import BENCH
from rehearse import call, finish, insert_fixture, new_context
from werkzeug.serving import make_server

from ucm_migration_loader.contract import digest


@contextmanager
def publication_db(socket, context, ids, work, member_role=None, additional_ids=()):
    """Use only a separate, socket-only publisher. Retain the fixture, revoke its ephemeral login."""
    schema = "ucm_snapshot_" + uuid4().hex
    with (
        pymysql.connect(
            unix_socket=str(socket), user=getpass.getuser(), autocommit=True
        ) as db,
        db.cursor() as cur,
    ):
        lock = "ucm_publisher_" + uuid4().hex
        cur.execute("select get_lock(%s, 0), @@skip_networking", lock)
        if cur.fetchone() != (1, 1):
            raise ValueError("Use a local socket-only publishing server")
        if frappe.db.sql("select is_used_lock(%s)", lock)[0][0] is not None:
            raise ValueError(
                "The publisher and target must be separate MariaDB servers"
            )
        cur.execute("select release_lock(%s)", lock)
        cur.execute(f"create database `{schema}` character set utf8mb4")
        db.select_db(schema)
        for dt in ("Person", "Person Other Name", "Case", "Case Member"):
            ddl = frappe.db.sql(f"show create table `tab{dt}`")[0][1]
            cur.execute(ddl)
        insert_fixture(cur, ids, context)
        for extra in additional_ids:
            insert_fixture(cur, extra, context)
        if member_role:
            cur.execute(
                "update `tabCase Member` set person_member_role=%s where name=%s",
                (member_role, ids["member"]),
            )
        with publication_reader(cur, schema, socket, work) as config:
            yield schema, config


@contextmanager
def publication_reader(cur, schema, socket, work):
    reader = "ucm_read_" + uuid4().hex[:16]
    password = secrets.token_urlsafe(32)
    cur.execute("create user %s@'localhost' identified by %s", (reader, password))
    try:
        db_pattern = (
            schema.replace("\\", "\\\\")
            .replace("_", "\\_")
            .replace("%", "\\%")
            .replace("`", "``")
        )
        cur.execute(f"grant select on `{db_pattern}`.* to %s@'localhost'", reader)
        config = work / "publisher.json"
        config.write_text(
            json.dumps(
                {
                    "unix_socket": str(socket),
                    "user": reader,
                    "password": password,
                    "database": schema,
                }
            )
        )
        config.chmod(0o600)
        yield config
    finally:
        cur.execute("drop user %s@'localhost'", reader)


@contextmanager
def tls_site(site, work, *, threaded=False):
    """Expose the actual Frappe WSGI app on a loopback TLS listener for this test only."""
    import frappe.app

    cert, key = work / "localhost.pem", work / "localhost.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    old_site = frappe.app._site
    frappe.app._site = site
    try:
        with make_server(
            "127.0.0.1",
            0,
            frappe.app.application,
            threaded=threaded,
            ssl_context=(str(cert), str(key)),
        ) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                yield f"https://localhost:{server.server_port}", cert
            finally:
                server.shutdown()
                thread.join()
    finally:
        frappe.app._site = old_site


@contextmanager
def publisher_identity():
    from frappe.core.doctype.user.user import generate_keys
    from frappe.utils.password import remove_encrypted_password

    email = f"ucm-push-{uuid4().hex}@example.invalid"
    account = frappe.get_doc(
        {
            "doctype": "User",
            "email": email,
            "first_name": "Synthetic Publisher",
            "user_type": "Website User",
            "send_welcome_email": 0,
            "restrict_ip": "127.0.0.1",
            "sso_api_key_allowed": 1,
        }
    )
    account.insert()
    try:
        keys = generate_keys(email)
        frappe.db.commit()
        yield email, keys["api_key"] + ":" + keys["api_secret"]
    finally:
        frappe.db.rollback()
        if frappe.db.exists("User", email):
            account = frappe.get_doc("User", email)
            account.enabled = 0
            account.api_key = ""
            account.api_secret = ""
            account.save()
            remove_encrypted_password("User", email, "api_secret")
            frappe.db.commit()


def run(site, socket, work):
    context = new_context("synthetic-https").name
    ids = {k: str(uuid4()) for k in ("person", "case", "other", "member")}
    with publication_db(socket, context, ids, work) as (schema, config):
        print(json.dumps(transfer(site, context, ids, work, schema, config)))


def transfer(
    site,
    context,
    ids,
    work,
    schema,
    config,
    attempt=1,
    on_staged=None,
    member_role=None,
):
    with (
        tls_site(site, work) as (url, cert),
        publisher_identity() as (publisher, token),
        publisher_identity() as (_, other_token),
    ):
        base = ["--site", site, "--url", url]
        source = [
            "--snapshot",
            schema,
            "--context",
            context,
            "--source-config",
            str(config),
            "--batch-size",
            "1",
            "--attempt",
            str(attempt),
        ]
        env = {
            **os.environ,
            "NO_PROXY": "localhost",
            "no_proxy": "localhost",
            "REQUESTS_CA_BUNDLE": str(cert),
            "UCM_MIGRATION_TOKEN": token,
        }

        def cli(action, *opts, expected_code=0):
            result = subprocess.run(
                [str(BENCH / "env/bin/ucm-push"), action, *base, *opts],
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != expected_code:
                raise RuntimeError(f"CLI {action} failed: {result.stderr[-500:]}")
            return json.loads(result.stdout)

        manifest_path = work / "manifest.json"
        planned = cli("plan", *source, "--manifest-out", str(manifest_path))
        manifest = json.loads(manifest_path.read_text())
        run_id = planned["run"]
        assert planned["documents"] == 3 and planned["batches"] == 3
        assert planned["rows_by_type"] == {
            "Person": 1,
            "Person Other Name": 1,
            "Case": 1,
            "Case Member": 1,
        }
        wrong_schema = {**manifest, "schema": {**manifest["schema"], "Case": "0" * 64}}
        try:
            call("begin", manifest=wrong_schema, publishing_user=publisher)
        except ValueError as exc:
            assert "DDL differ for Case" in str(exc)
        else:
            raise AssertionError(
                "A publication with a different Case definition was accepted"
            )

        def post(method, args, credential=token):
            return requests.post(
                f"{url}/api/method/ucm_migration_loader.api.{method}",
                json=args,
                headers={"Authorization": f"token {credential}"} if credential else {},
                verify=str(cert),
                timeout=10,
            )

        assert post("begin", {"manifest": manifest}).status_code == 403
        prepared = subprocess.run(
            [
                "bench",
                "--site",
                site,
                "execute",
                "ucm_migration_loader.operator.prepare_publication",
                "--kwargs",
                json.dumps(
                    {"manifest_path": str(manifest_path), "publishing_user": publisher}
                ),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert json.loads(prepared.stdout)["run"] == run_id
        assert post("status", {"run": run_id}, credential=None).status_code == 403
        assert (
            post("status", {"run": run_id}, credential=other_token).status_code == 403
        )
        for method in ("verify", "apply", "pause", "activate", "discard"):
            assert post(method, {"run": run_id}).status_code == 403
        first = cli("push", *source, "--max-batches", "1")
        assert first["received"] == 1 and first["state"] == "Receiving", first
        # Duplicate delivery after a lost acknowledgment. Reconstruct from the persisted publication.
        from ucm_migration_loader.push import publication

        with pymysql.connect(**json.loads(config.read_text())) as conn:
            docs = [next(publication(conn, context))]
        assert (
            post(
                "upload",
                {"run": run_id, "sequence": 1, "documents": docs},
                credential=other_token,
            ).status_code
            == 403
        )
        replay = post("upload", {"run": run_id, "sequence": 1, "documents": docs})
        replay.raise_for_status()
        assert replay.json()["message"] == {"sequence": 1, "digest": digest(docs)}
        assert cli("status", "--run", run_id)["received"] == 1
        resumed = cli("push", *source)
        assert resumed["received"] == 3 and resumed["state"] == "Staged", resumed
        assert cli("push", *source)["received"] == 3
        if on_staged is not None:
            on_staged()

        def review():
            # Construct the expected business mapping from the fixture specification, independent
            # of the publication reader and its target projection.
            expected = {
                "site": site,
                "snapshot": schema,
                "context": context,
                "entities": {
                    "Person": [
                        {
                            "name": ids["person"],
                            "context_tech_id": context,
                            "person_name": "Pilot Person",
                            "person_file_id": "SYN-" + ids["person"][:8],
                        }
                    ],
                    "Case": [
                        {
                            "name": ids["case"],
                            "context_tech_id": context,
                            "case_name": "Pilot Case",
                            "case_status": "Open",
                            "case_number": "SYN-" + ids["case"][:8],
                        }
                    ],
                    "Case Member": [
                        {
                            "name": ids["member"],
                            "context_tech_id": context,
                            "case_tech_id": ids["case"],
                            "person_tech_id": ids["person"],
                            "person_member_sequence_number": 1,
                            "person_member_role": member_role,
                        }
                    ],
                    "Person Other Name": [
                        {
                            "name": ids["other"],
                            "parent": ids["person"],
                            "idx": 1,
                            "person_other_name": "Pilot alias",
                        }
                    ],
                },
            }
            file = work / "expected.json"
            member = expected["entities"]["Case Member"][0]
            member["person_tech_id"] = str(uuid4())
            file.write_text(json.dumps(expected))
            mismatch = cli(
                "reconcile", "--run", run_id, "--expected", str(file), expected_code=1
            )
            assert mismatch["differences"] == {
                "Case Member": ["matched_fields_sha256"]
            }, mismatch
            member["person_tech_id"] = ids["person"]
            file.write_text(json.dumps(expected))
            assert cli("reconcile", "--run", run_id, "--expected", str(file))["matched"]

        finish(run_id, ids, apply_batches=True, review=review)
        assert cli("status", "--run", run_id)["state"] == "Activated"
        evidence = cli("report", "--run", run_id)
        assert evidence["activated_by"] and evidence["activated_on"]
        assert {
            dt: row["rows"]
            for dt, row in evidence["reconciliation"]["entities"].items()
        } == planned["rows_by_type"]
        assert (
            post("report", {"run": run_id}, credential=other_token).status_code == 403
        )
        return {
            "site": site,
            "source_database": schema,
            "context": context,
            "run": run_id,
            "staged_before_restart": 1,
            "staged_after_resume": 3,
            "state": "Activated",
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", required=True)
    parser.add_argument("--publisher-socket", type=Path, required=True)
    args = parser.parse_args()
    socket = args.publisher_socket.resolve()
    os.chdir(BENCH / "sites")
    frappe.init(site=args.site)
    frappe.connect()
    try:
        with tempfile.TemporaryDirectory(prefix="ucm-transport-") as folder:
            run(args.site, socket, Path(folder))
    finally:
        frappe.destroy()


if __name__ == "__main__":
    main()
