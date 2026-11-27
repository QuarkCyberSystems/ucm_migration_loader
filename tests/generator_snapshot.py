"""Independent, read-only expected results for delegation records in a generated UCM publication."""

import argparse
import hashlib
import json
import os
from datetime import date, time, timedelta
from decimal import Decimal
from pathlib import Path

import frappe
import pymysql
from rehearsal_bench import BENCH

FORMAT = "ucm-generator-delegation-v1"
AUDIT = {"creation", "modified", "owner", "modified_by"}
# Select by the owning record, even when the stored fetched context is NULL or the child names another delegation.
PARENTS = {
	"Case Member": ("Case", "case_tech_id"),
	"Activity Case": ("Case", "case_tech_id"),
	"Activity Detail": ("Activity", "activity_tech_id"),
	"Person Relationship": ("Person", "person_tech_id"),
	"Person Note": ("Person", "person_tech_id"),
	"Activity Note": ("Activity", "activity_tech_id"),
	"Activity Detail Note": ("Activity", "activity_tech_id"),
	"Person Other Name": ("Person", "parent"),
	"Person Address": ("Person", "parent"),
	"Activity Objective": ("Activity", "parent"),
	"Context Case": ("Case", "parent"),
	"Person Consent": ("Person", "person_tech_id"),
}
DIRECT = ("Organization", "Person", "Case", "Activity", "Record Tag", "Record Share")
DYNAMIC = {
	"Sensitive Information": (
		"record_doctype",
		"record_name",
		("Organization", "Person", "Activity", "Activity Detail"),
	),
	"ToDo": ("reference_type", "reference_name", ("Person", "Case")),
	"File": ("attached_to_doctype", "attached_to_name", ("Person",)),
}


def ownership(dt):
	if dt in PARENTS:
		parent, link = PARENTS[dt]
		return (
			f"join `tab{parent}` p on p.name=r.`{link}` where p.context_tech_id=%s",
			1,
		)
	if dt in DIRECT:
		return "where r.context_tech_id=%s", 1
	kind, link, parents = DYNAMIC[dt]
	predicates = [
		f"(r.`{kind}`='{parent}' and exists (select 1 from `tab{parent}` p where p.name=r.`{link}` and p.context_tech_id=%s))"
		for parent in parents
	]
	return "where " + " or ".join(predicates), len(parents)


def scalar(value):
	if isinstance(value, (date, time)):
		return value.isoformat()
	if isinstance(value, (Decimal, timedelta)):
		return str(value)
	if isinstance(value, bytes):
		return {"hex": value.hex()}
	raise TypeError(f"Unsupported MariaDB value of type {type(value).__name__}")


def frame(value):
	return (
		json.dumps(
			value,
			default=scalar,
			ensure_ascii=False,
			allow_nan=False,
			separators=(",", ":"),
		).encode()
		+ b"\n"
	)


def file_digest(url, private_root):
	prefix = "/private/files/"
	if not isinstance(url, str) or not url.startswith(prefix):
		raise ValueError("Generator attachment must have a local private file")
	path = (private_root / url[len(prefix) :]).resolve(strict=True)
	if not path.is_relative_to(private_root) or not path.is_file():
		raise ValueError("Attachment outside private file storage")
	with path.open("rb") as handle:
		return hashlib.file_digest(handle, "sha256").hexdigest()


def fingerprint(conn, dt, context, private_root):
	predicate, nparams = ownership(dt)
	# Published snapshots use a streaming PyMySQL connection. A rollback-only ORM
	# rehearsal must read its own uncommitted rows using the bench's native driver.
	cursor = (
		conn.cursor(pymysql.cursors.SSCursor)
		if isinstance(conn, pymysql.connections.Connection)
		else conn.cursor()
	)
	with cursor as cur:
		cur.execute(
			f"select r.* from `tab{dt}` r {predicate} order by r.name",
			(context,) * nparams,
		)
		columns = [col[0] for col in cur.description]
		fields = sorted(set(columns) - AUDIT)
		positions = [columns.index(f) for f in fields]
		audit_fields = ["name", *sorted(AUDIT)]
		audit_positions = [columns.index(f) for f in audit_fields]
		data = hashlib.sha256(frame(fields))
		audit = hashlib.sha256(frame(audit_fields))
		content = hashlib.sha256()
		count = 0
		for row in cur:
			data.update(frame([row[i] for i in positions]))
			audit.update(frame([row[i] for i in audit_positions]))
			if dt == "File":
				if row[columns.index("is_private")] != 1:
					raise ValueError("Generated person attachment is public")
				content.update(
					frame(
						[
							row[columns.index("name")],
							file_digest(row[columns.index("file_url")], private_root),
						]
					)
				)
			count += 1
	result = {
		"rows": count,
		"stored_fields_sha256": data.hexdigest(),
		"audit_fields_sha256": audit.hexdigest(),
	}
	if dt == "File":
		result["attachment_bytes_sha256"] = content.hexdigest()
	return result


def capture(conn, contexts, private_root):
	with conn.cursor() as cur:
		cur.execute("set transaction isolation level repeatable read")
		cur.execute("start transaction with consistent snapshot, read only")
		waves = {}
		for context in contexts:
			cur.execute("select context_code from tabContext where name=%s", (context,))
			record = cur.fetchone()
			if not record:
				raise ValueError(f"Missing expected delegation {context}")
			waves[context] = {
				"context_code": record[0],
				"tables": {
					dt: fingerprint(conn, dt, context, private_root)
					for dt in sorted((*PARENTS, *DIRECT, *DYNAMIC))
				},
			}
	return {"format": FORMAT, "waves": waves}


def compare(expected, actual):
	if expected.get("format") != FORMAT or actual.get("format") != FORMAT or not expected.get("waves"):
		raise ValueError("Invalid generator snapshot format or empty wave list")
	if set(expected["waves"]) != set(actual["waves"]):
		raise ValueError("Delegation set differs")
	findings = {}
	for context, before in expected["waves"].items():
		after = actual["waves"][context]
		if before["context_code"] != after["context_code"] or set(before["tables"]) != set(after["tables"]):
			raise ValueError(f"Delegation identity or table set differs for {context}")
		differences = {
			dt: sorted(
				k for k in set(ref) | set(after["tables"][dt]) if ref.get(k) != after["tables"][dt].get(k)
			)
			for dt, ref in before["tables"].items()
		}
		if changed := {dt: keys for dt, keys in differences.items() if keys}:
			findings[context] = changed
	return findings


def site_connection():
	cfg = frappe.conf
	return pymysql.connect(
		unix_socket=cfg.db_socket,
		host=cfg.db_host or "localhost",
		port=cfg.db_port or 3306,
		user=cfg.db_user or cfg.db_name,
		password=cfg.db_password,
		database=cfg.db_name,
	)


def site_snapshot(contexts):
	with site_connection() as conn:
		return capture(conn, contexts, Path(frappe.get_site_path("private", "files")).resolve())


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--site", required=True)
	parser.add_argument(
		"--expected",
		required=True,
		type=Path,
		help="Evidence from generator_coverage.py",
	)
	args = parser.parse_args()
	expected = json.loads(args.expected.read_text())["delegation_snapshot"]
	os.chdir(BENCH / "sites")
	frappe.init(site=args.site)
	try:
		findings = compare(expected, site_snapshot(expected["waves"]))
		print(json.dumps({"site": args.site, "matched": not findings, "differences": findings}))
		raise SystemExit(bool(findings))
	finally:
		frappe.destroy()
