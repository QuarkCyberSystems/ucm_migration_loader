"""Compare the stored pilot records with the frozen publication without exposing case data in reports."""

import json
from collections import defaultdict

import frappe

from .contract import OTHER_NAMES, PROFILES
from .evidence import entity_summary


def project(source, stored, fields):
	# The pilot lets UCM assign defaults for unspecified values. UUIDs may be native database values.
	expected = {key: str(source[key]) for key in fields if source.get(key) not in (None, "")}
	actual = {key: str(stored.get(key)) for key in expected}
	if expected != actual:
		field = next(key for key in expected if expected[key] != actual[key])
		raise ValueError(f"Published {stored.doctype}.{field} differs on the target")
	return actual


def summarize(record):
	from .application import check_receipt

	fields = PROFILES[json.loads(record.manifest_json)["profile"]]
	matches = defaultdict(list)
	for sequence in range(1, record.received + 1):
		for doc in check_receipt(record, sequence):
			dt, name = doc["doctype"], doc["name"]
			stored = frappe.get_doc(dt, name, for_update=True)
			matches[dt].append(project(doc, stored, ("name", "context_tech_id", *fields[dt])))
			if dt == "Person":
				children = doc.get("person_other_names", [])
				if len(stored.person_other_names) != len(children):
					raise ValueError("Person Other Name count differs on the target")
				for source, child in zip(children, stored.person_other_names, strict=True):
					# Fetching through the parent proves ownership. Include it in the relationship hash.
					row = project(source, child, ("name", "idx", *OTHER_NAMES))
					matches["Person Other Name"].append({"parent": name, **row})

	for dt in fields:
		if frappe.db.count(dt, {"context_tech_id": record.context_id}) != len(matches[dt]):
			raise ValueError(f"Target count differs for {dt}")
	if sum(len(matches[dt]) for dt in fields) != record.document_count:
		raise ValueError("Reconciled document count differs from the run receipt")

	entities = {dt: entity_summary(matches[dt]) for dt in (*fields, "Person Other Name")}
	return {
		"run": record.name,
		"site": frappe.local.site,
		"snapshot": record.snapshot_id,
		"context": record.context_id,
		"batches": record.received,
		"entities": entities,
	}
