"""Rehearse generated alternate names and consent through the target's normal UCM controllers."""

import argparse
import json
import os

import frappe
from generator_cases import project
from generator_collisions import read_site
from generator_snapshot import BENCH

from ucm_migration_loader.api import PILOT_MARKER
from ucm_migration_loader.consent_publication import (
	CONSENT_FIELDS,
	WITHHELD,
	consent_documents,
)
from ucm_migration_loader.parent_publication import ALIAS_FIELDS, person_name_documents


def exercise(source, target):
	from ucm.permissions.sharing import (
		SHARE_RECIPIENT,
		WITHHELD_FIELDS,
		consent,
		withheld_fields,
	)

	if not target.startswith("ucm-") or not target.endswith(".localhost") or source == target:
		raise ValueError("Use a separate local synthetic rehearsal site")
	with read_site(source) as pub:
		with pub.cursor() as c:
			c.execute("select name, context_code from tabContext order by context_code")
			contexts = c.fetchall()
		frappe.init(site=target)
		frappe.connect()
		try:
			result = {}
			for context, code in contexts:
				frappe.get_doc(
					{
						"doctype": "Context",
						"context_name": "Generator rehearsal " + code,
						"context_code": code,
						"context_status": "Locked",
						"context_discoverability": "Hidden",
						"context_description": PILOT_MARKER,
					}
				).insert(set_name=context)
				persons = aliases = decisions = 0
				for person in person_name_documents(pub, context):
					expected_names = person.pop("person_all_names")
					frappe.get_doc(person).insert(set_name=person["name"], set_child_names=False)
					saved = frappe.get_doc("Person", person["name"])
					assert saved.person_all_names == expected_names
					for child, expected in zip(
						saved.person_other_names,
						person["person_other_names"],
						strict=True,
					):
						assert project(child, ALIAS_FIELDS) == project(expected, ALIAS_FIELDS)
						assert str(child.parent) == person["name"]
						aliases += 1
					persons += 1
				for expected in consent_documents(pub, context):
					frappe.get_doc(expected).insert(set_name=expected["name"])
					saved = frappe.get_doc("Person Consent", expected["name"])
					assert project(saved, CONSENT_FIELDS) == project(expected, CONSENT_FIELDS)
					effective = consent(expected["person_tech_id"], expected["consent_recipient"])
					assert project(effective, ("consent_decision", *WITHHELD)) == project(
						expected, ("consent_decision", *WITHHELD)
					)
					if expected["consent_recipient"] == SHARE_RECIPIENT:
						wanted = set()
						for flag in WITHHELD:
							if expected["consent_decision"] == "Total objection" or (
								expected["consent_decision"] == "Partial objection" and expected[flag]
							):
								wanted.update(WITHHELD_FIELDS[flag])
						assert withheld_fields(expected["person_tech_id"]) == wanted
					decisions += 1
				result[code] = {
					"person_name_projections": persons,
					"other_names": aliases,
					"consents": decisions,
				}
			if not sum(w["other_names"] for w in result.values()) or not sum(
				w["consents"] for w in result.values()
			):
				raise ValueError(
					"Use a generator publication with both alternate names and consent decisions"
				)
			frappe.db.rollback()
			assert all(not frappe.db.exists("Context", ctx) for ctx, _ in contexts)
			return result
		finally:
			frappe.db.rollback()
			frappe.destroy()


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--source-site", required=True)
	parser.add_argument("--target", required=True)
	args = parser.parse_args()
	os.chdir(BENCH / "sites")
	print(
		json.dumps(
			{
				"target": args.target,
				"rolled_back": exercise(args.source_site, args.target),
			}
		)
	)
