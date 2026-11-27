"""Versioned, bounded case-file pilot. Shared by the standalone client and Frappe."""

import json
import re
from hashlib import sha256
from uuid import UUID

PROFILE = "synthetic-case-file-v1"
MAX_DOCUMENTS = 500
MAX_PUBLICATION_BATCH_DOCUMENTS = 1000
MAX_BYTES = 256 * 1024
MAX_RUN_BYTES = 4 * 1024 * 1024
FIELDS = {
	"Person": ("person_name", "person_file_id", "person_gender"),
	"Case": ("case_name", "case_number", "case_status"),
	"Case Member": (
		"case_tech_id",
		"person_tech_id",
		"person_member_role",
		"person_member_sequence_number",
	),
}
OTHER_NAMES = (
	"person_other_name",
	"person_other_name_type",
	"person_other_name_remark",
)
TABLES = (*FIELDS, "Person Other Name")


WITHHELD = (
	"is_notes_withheld",
	"is_photo_withheld",
	"is_documents_withheld",
	"is_location_withheld",
	"is_contact_withheld",
)
CONSENT_FIELDS = (
	"name",
	"context_tech_id",
	"person_tech_id",
	"consent_recipient",
	"consent_decision",
	"consent_given_by",
	"consent_date",
	"consent_remark",
	*WITHHELD,
)


CONSENT_PROFILE = "synthetic-case-file-consent-v2"
PROFILES = {
	PROFILE: FIELDS,
	CONSENT_PROFILE: {**FIELDS, "Person Consent": CONSENT_FIELDS[2:]},
}


def schema_signatures(read_ddl, profile=PROFILE):
	"""Fingerprint the deployed MariaDB definitions, excluding the mutable next AUTO_INCREMENT."""
	return table_signatures(read_ddl, (*PROFILES[profile], "Person Other Name"))


def table_signatures(read_ddl, doctypes, *, sort_indexes=False):
	signatures = {}
	for dt in doctypes:
		ddl = read_ddl(f"show create table `tab{dt}`").replace("\r\n", "\n")
		columns, separator, options = ddl.rpartition("\n)")
		if sort_indexes:
			definitions = columns.splitlines()

			def is_index(line):
				return line.lstrip().startswith(
					(
						"KEY ",
						"UNIQUE KEY ",
						"FULLTEXT KEY ",
						"SPATIAL KEY ",
						"PRIMARY KEY ",
					)
				)

			ordered = [line.rstrip(",") for line in definitions[1:] if not is_index(line)]
			indexes = sorted(line.rstrip(",") for line in definitions[1:] if is_index(line))
			columns = definitions[0] + "\n" + ",\n".join([*ordered, *indexes])
		options = re.sub(r"\bAUTO_INCREMENT=\d+\s*", "", options, count=1)
		signatures[dt] = sha256((columns + separator + options).encode()).hexdigest()
	return signatures


def encoded(value):
	return json.dumps(
		value,
		sort_keys=True,
		separators=(",", ":"),
		ensure_ascii=False,
		allow_nan=False,
	).encode()


def digest(value):
	return sha256(encoded(value)).hexdigest()


def check_uuid(value):
	if not isinstance(value, str) or str(UUID(value)) != value:
		raise ValueError("Expected a canonical UUID")


def check_manifest(manifest):
	if (
		set(manifest) - {"profile", "snapshot", "site", "context", "batches", "attempt", "schema"}
		or not {"profile", "snapshot", "site", "context", "batches"}.issubset(manifest)
		or manifest["profile"] not in PROFILES
	):
		raise ValueError("Unrecognized manifest")
	check_uuid(manifest["context"])
	if "schema" in manifest:
		schema = manifest["schema"]
		if (
			not isinstance(schema, dict)
			or set(schema) != {*PROFILES[manifest["profile"]], "Person Other Name"}
			or not all(isinstance(h, str) and re.fullmatch(r"[0-9a-f]{64}", h) for h in schema.values())
		):
			raise ValueError("Invalid publication schema fingerprints")
	attempt = manifest.get("attempt", 1)
	if type(attempt) is not int or attempt < 1:
		raise ValueError("Attempt must be a positive integer")
	for key in ("site", "snapshot"):
		if not isinstance(manifest[key], str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", manifest[key]):
			raise ValueError(f"Invalid {key}")
	batches = manifest["batches"]
	if not isinstance(batches, list) or not 1 <= len(batches) <= MAX_DOCUMENTS:
		raise ValueError("Expected a nonempty, bounded list of batch digests")
	if any(not isinstance(h, str) or not re.fullmatch(r"[0-9a-f]{64}", h) for h in batches):
		raise ValueError("Invalid batch digest")


def check_documents(docs, context, profile=PROFILE):
	fields = PROFILES[profile]
	if not isinstance(docs, list) or not docs or len(docs) > MAX_DOCUMENTS or len(encoded(docs)) > MAX_BYTES:
		raise ValueError("Batch exceeds the pilot limits or is empty")
	for doc in docs:
		if not isinstance(doc, dict) or doc.get("doctype") not in fields:
			raise ValueError("Unsupported pilot document")
		dt = doc["doctype"]
		allowed = set(fields[dt]) | {"doctype", "name", "context_tech_id"}
		if dt == "Person":
			allowed.add("person_other_names")
		if set(doc) - allowed or doc.get("context_tech_id") != context:
			raise ValueError("Unexpected fields or context")
		check_uuid(doc.get("name"))
		if dt == "Person Consent":
			check_uuid(doc.get("person_tech_id"))
			if (
				not doc.get("consent_recipient")
				or not doc.get("consent_decision")
				or any(type(doc.get(f)) is not int or doc[f] not in (0, 1) for f in WITHHELD)
			):
				raise ValueError("Consent requires a decision, recipient and all binary withholding flags")
		if dt == "Case Member":
			for field in ("case_tech_id", "person_tech_id"):
				check_uuid(doc.get(field))
		for field in fields[dt]:
			if doc.get(field) is not None and type(doc[field]) not in (int, str):
				raise ValueError(f"Invalid scalar for {dt}.{field}")
		children = doc.get("person_other_names", [])
		if not isinstance(children, list):
			raise TypeError("Expected a list of other names")
		for i, child in enumerate(children, 1):
			if (
				not isinstance(child, dict)
				or set(child) - {"name", "idx", *OTHER_NAMES}
				or child.get("idx") != i
			):
				raise ValueError("Invalid other-name fields or order")
			check_uuid(child["name"])
			if any(child.get(f) is not None and not isinstance(child[f], str) for f in OTHER_NAMES):
				raise ValueError("Invalid other-name value")


def plan(
	site,
	snapshot,
	context,
	documents,
	size=100,
	byte_limit=MAX_BYTES,
	attempt=1,
	profile=PROFILE,
):
	if not 1 <= size <= MAX_DOCUMENTS or not 1 <= byte_limit <= MAX_BYTES:
		raise ValueError("Invalid batch limits")
	batches, batch = [], []
	last_type = None
	seen = set()
	for doc in documents:
		check_documents([doc], context, profile)
		key = doc["doctype"], doc["name"]
		if key in seen or len(seen) >= MAX_DOCUMENTS:
			raise ValueError("Duplicate document or pilot document limit reached")
		seen.add(key)
		if doc["doctype"] == "Person Consent" and ("Person", doc["person_tech_id"]) not in seen:
			raise ValueError("Missing Person prerequisite for consent")
		if doc["doctype"] == "Case Member":
			for dt, field in (("Case", "case_tech_id"), ("Person", "person_tech_id")):
				if (dt, doc[field]) not in seen:
					raise ValueError(f"Missing {dt} prerequisite for {doc['name']}")
		if len(encoded([doc])) > byte_limit:
			raise ValueError(f"Document exceeds byte limit: {doc['name']}")
		if batch and (
			last_type != doc["doctype"] or len(batch) >= size or len(encoded([*batch, doc])) > byte_limit
		):
			batches.append(batch)
			batch = []
		batch.append(doc)
		last_type = doc["doctype"]
	if batch:
		batches.append(batch)
	if sum(len(encoded(b)) for b in batches) > MAX_RUN_BYTES:
		raise ValueError("Run exceeds the pilot byte limit")
	manifest = {
		"profile": profile,
		"site": site,
		"snapshot": snapshot,
		"context": context,
		"batches": list(map(digest, batches)),
	}
	if attempt != 1:
		manifest["attempt"] = attempt
	check_manifest(manifest)
	return manifest, batches
