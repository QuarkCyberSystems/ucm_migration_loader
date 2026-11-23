"""Apply a generator-derived person/consent projection, resume it, reconcile and discard."""

import argparse
import json
import os

import frappe
from generator_collisions import read_site
from generator_snapshot import BENCH
from rehearse import call, new_context, stage, worker
from ucm_migration_loader.consent_publication import consent_documents
from ucm_migration_loader.contract import CONSENT_PROFILE, plan
from ucm_migration_loader.evidence import compare_expected
from ucm_migration_loader.push import publisher_schema


def run(source, target):
    if (
        source == target
        or not target.startswith("ucm-")
        or not target.endswith(".localhost")
    ):
        raise ValueError("Use a distinct local synthetic rehearsal site")
    with read_site(source) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select context_tech_id from `tabPerson Consent` order by name limit 1"
            )
            original_context = cur.fetchone()[0]
        consents = list(consent_documents(conn, original_context))[:5]
        assert consents
        schema = publisher_schema(conn, CONSENT_PROFILE)
        persons = []
        with conn.cursor() as cur:
            for name in sorted({c["person_tech_id"] for c in consents}):
                cur.execute(
                    "select person_name,person_file_id from tabPerson where name=%s",
                    name,
                )
                person_name, file_id = cur.fetchone()
                persons.append(
                    {
                        "doctype": "Person",
                        "name": name,
                        "person_name": person_name,
                        "person_file_id": file_id,
                    }
                )
    frappe.init(site=target)
    frappe.connect()
    try:
        context = new_context("consent-worker").name
        documents = [*persons, *consents]
        for doc in documents:
            doc["context_tech_id"] = context
        manifest, batches = plan(
            target,
            "generator-consent",
            context,
            documents,
            size=2,
            profile=CONSENT_PROFILE,
        )
        manifest["schema"] = schema
        rid = stage(manifest, batches)
        call("verify", run=rid)
        worker()
        assert call("status", run=rid)["state"] == "Verified"
        call("apply", run=rid, max_batches=1)
        worker()
        assert call("status", run=rid)["applied"] == 1
        call("pause", run=rid)
        assert call("status", run=rid)["state"] == "Paused"
        call("apply", run=rid)
        worker()
        report = call("report", run=rid)
        entities = {dt: [] for dt in schema}
        for doc in documents:
            entities[doc["doctype"]].append(
                {k: v for k, v in doc.items() if k != "doctype"}
            )
        expected = {
            "site": target,
            "context": context,
            "snapshot": manifest["snapshot"],
            "entities": entities,
        }
        assert compare_expected(expected, report)["matched"]
        call("discard", run=rid)
        worker()
        assert call("status", run=rid)["state"] == "Discarded"
        assert all(
            not frappe.db.exists(doc["doctype"], doc["name"]) for doc in documents
        )
        return {
            "run": rid,
            "consents": len(consents),
            "persons": len(persons),
            "batches": len(batches),
            "state": "Discarded",
        }
    finally:
        frappe.destroy()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-site", required=True)
    p.add_argument("--target", required=True)
    args = p.parse_args()
    os.chdir(BENCH / "sites")
    print(json.dumps(run(args.source_site, args.target)))
