"""Check UCM search, report, export and private pictures with a real caseworker session."""

import argparse
import csv
import hashlib
import io
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from uuid import uuid4

import frappe
from first_wave import caseworker_session
from generator_snapshot import BENCH
from requests import HTTPError
from ucm_migration_loader.publication_receiver import local_publication


def published_items(run):
    with sqlite3.connect(
        local_publication(run).resolve().as_uri() + "?mode=ro", uri=True
    ) as db:
        for (payload,) in db.execute("select payload from batches order by sequence"):
            yield from json.loads(payload)


def examples(run):
    case_names, shared = [], set()
    photos = []
    for item in published_items(run):
        dt, row = item["doctype"], item["row"]
        if dt == "Case" and not row.get("case_tags"):
            case_names.append(row["name"])
        if dt == "Record Share" and row["share_status"] in ("Accepted", "Pending"):
            shared.add((row["share_doctype"], row["share_name"]))
        if dt == "Person" and item["files"] and not row.get("person_tags"):
            f = item["files"][0]
            photos.append((row["name"], f["row"]["name"], f["sha256"]))
    case = next(key for key in case_names if ("Case", key) not in shared)
    return case, [
        (key, digest)
        for person, key, digest in photos
        if ("Person", person) not in shared
    ]


def check(request, first, second, *, export_allowed=False):
    own_case, own_files = examples(first)
    other_case, other_files = examples(second)
    # Frappe scopes a private download to the exact File record when fid is supplied.
    other_file = other_files[0]
    for key, visible in ((own_case, True), (other_case, False)):
        assert frappe.db.exists("Case", key)
        filters = json.dumps({"name": key})
        results = request(
            "GET",
            "/api/method/frappe.desk.search.search_link",
            params={"doctype": "Case", "txt": key, "filters": filters},
        )["message"]
        assert [r["value"] for r in results] == ([key] if visible else [])
        args = {"doctype": "Case", "filters": filters, "fields": json.dumps(["name"])}
        rows = request(
            "GET", "/api/method/frappe.desk.reportview.get_list", params=args
        )["message"]
        assert [r["name"] for r in rows] == ([key] if visible else [])
        count = request(
            "GET",
            "/api/method/frappe.desk.reportview.get_count",
            params={"doctype": "Case", "filters": filters},
        )["message"]
        assert count == int(visible)
    # Export the two exact keys together. The response must retain the accessible row.
    try:
        csv_bytes = request(
            "GET",
            "/api/method/frappe.desk.reportview.export_query",
            binary=True,
            params={
                "doctype": "Case",
                "fields": json.dumps(["name"]),
                "filters": json.dumps({"name": ["in", [own_case, other_case]]}),
                "file_format_type": "CSV",
            },
        )
    except HTTPError as exc:
        assert not export_allowed and exc.response.status_code == 403
    else:
        assert export_allowed
        values = {
            cell
            for row in csv.reader(io.StringIO(csv_bytes.decode("utf-8-sig")))
            for cell in row
        }
        assert own_case in values and other_case not in values
    for (file, digest), visible in ((own_files[0], True), (other_file, False)):
        url = frappe.db.get_value("File", file, "file_url")
        assert url and url.startswith("/private/files/")
        try:
            data = request("GET", url, params={"fid": file}, binary=True)
        except HTTPError as exc:
            assert not visible and exc.response.status_code == 403
        else:
            assert visible and hashlib.sha256(data).hexdigest() == digest
    return {
        "link_search": True,
        "report_list": True,
        "report_count": True,
        "csv_export": "own_case_only" if export_allowed else "permission_denied",
        "owned_picture_sha256_matches": True,
        "unshared_picture_denied": True,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("site", "first", "second", "evidence"):
        p.add_argument("--" + name, required=True)
    args = p.parse_args()
    out = Path(args.evidence).resolve()
    assert args.site.startswith("ucm-full-wave-") and not out.exists()
    os.chdir(BENCH / "sites")
    frappe.init(site=args.site)
    frappe.connect()
    try:
        from ucm.tests.utils import make_user

        ctx = frappe.db.get_value("UCM Migration Run", args.first, "context_id")
        user = make_user(f"wave-{uuid4().hex}@example.invalid", [(ctx, "Write")])
        frappe.db.commit()
        try:
            with (
                tempfile.TemporaryDirectory() as work,
                caseworker_session(user, Path(work)) as request,
            ):
                ordinary = check(request, args.first, args.second)
                team = frappe.db.get_value(
                    "Team", {"team_name": f"Contexts of {user}"}, "name"
                )
                profile = frappe.get_doc(
                    "Team Security Profile",
                    {"team_tech_id": team, "context_tech_id": ctx},
                )
                profile.profile_can_export_cases = 1
                profile.save()
                frappe.db.commit()
                permitted = check(request, args.first, args.second, export_allowed=True)
                result = {"ordinary_caseworker": ordinary, "case_exporter": permitted}
            with out.open("x") as handle:
                json.dump(
                    {
                        "site": args.site,
                        "second_state": frappe.db.get_value(
                            "UCM Migration Run", args.second, "state"
                        ),
                        **result,
                    },
                    handle,
                    indent=2,
                )
        finally:
            frappe.db.rollback()
            account = frappe.get_doc("User", user)
            account.enabled = 0
            account.save()
            frappe.db.commit()
    finally:
        frappe.destroy()
