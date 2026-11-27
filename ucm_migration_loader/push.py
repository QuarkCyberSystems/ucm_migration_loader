"""Read one frozen MariaDB publication and submit its batches to a UCM site."""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pymysql
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .contract import (
	CONSENT_PROFILE,
	FIELDS,
	MAX_BYTES,
	MAX_DOCUMENTS,
	OTHER_NAMES,
	PROFILE,
	PROFILES,
	check_code,
	check_uuid,
	digest,
	encoded,
	plan,
	schema_signatures,
)
from .scope import assess


def publisher_schema(connection, profile=PROFILE):
	with connection.cursor() as cursor:

		def read_ddl(sql):
			cursor.execute(sql)
			return cursor.fetchone()[1]

		return schema_signatures(read_ddl, profile)


def publication(connection, context, profile=PROFILE):
	"""Connection must expose the frozen UCM schema. Select only the pilot field inventory."""
	check_uuid(context)
	with connection.cursor(pymysql.cursors.DictCursor) as cursor:
		for dt, fields in FIELDS.items():
			columns = ", ".join(("name", "context_tech_id", *fields))
			if dt == "Case Member":
				# Membership belongs to its case, including publishing rows whose fetched context is NULL.
				columns = ", ".join(f"m.{col}" for col in ("name", *fields))
				cursor.execute(
					f"""select {columns}, m.context_tech_id as published_context,
                        c.context_tech_id from `tabCase Member` m left join tabCase c on c.name=m.case_tech_id
                        where c.context_tech_id=%s or m.context_tech_id=%s order by m.name limit %s""",
					(context, context, MAX_DOCUMENTS + 1),
				)
			else:
				cursor.execute(
					f"select {columns} from `tab{dt}` where context_tech_id=%s order by name limit %s",
					(context, MAX_DOCUMENTS + 1),
				)
			for row in cursor.fetchall():
				if row.pop("published_context", None) not in (None, context):
					raise ValueError("Case Member context differs from its case")
				if dt == "Person":
					cols = ", ".join(("name", "idx", *OTHER_NAMES))
					cursor.execute(
						f"""select {cols} from `tabPerson Other Name` where parent=%s
                        and parenttype='Person' and parentfield='person_other_names' order by idx, name""",
						row["name"],
					)
					row["person_other_names"] = list(cursor.fetchall())
				yield {"doctype": dt, **row}

	if profile == CONSENT_PROFILE:
		from .consent_publication import consent_documents

		yield from consent_documents(connection, context)


def send(call, manifest, batches, max_batches=None):
	receipt = call("begin", manifest=manifest)
	run = receipt["run"]
	if run != digest(manifest) or not 0 <= receipt["received"] <= len(batches):
		raise ValueError("Target receipt does not match this publication")
	if receipt.get("state") in ("Rejected", "Discarded"):
		raise ValueError(f"Run is {receipt['state']}")
	first = receipt["received"]
	stop = len(batches) if max_batches is None else min(first + max_batches, len(batches))
	for index in range(first, stop):
		result = call("upload", run=run, sequence=index + 1, documents=batches[index])
		if result["digest"] != manifest["batches"][index] or result["sequence"] != index + 1:
			raise ValueError("Unexpected batch receipt")
	return call("status", run=run)


def hand_over_quality(call, args):
	"""Fetch a run's metrics, store them on the migration side, and tell the run where they went."""
	from .ucm_migration_loader import data_quality

	document = call("quality", run=args.run)
	rows = document.get("metrics")
	if (
		document.get("format") != data_quality.FORMAT
		or (document.get("run"), document.get("site")) != (args.run, args.site)
		or not isinstance(rows, list)
		or digest(rows) != document.get("sha256")
	):
		raise ValueError("Target returned metrics of another run or changed in transit")
	destinations = []
	if args.quality_out:
		text = (
			data_quality.as_csv(document)
			if args.quality_out.suffix.lower() == ".csv"
			else json.dumps(document, indent=2) + "\n"
		)
		args.quality_out.write_text(text)
		destinations.append("file " + args.quality_out.name)
	if args.quality_config:
		config = json.loads(args.quality_config.read_text())
		table = args.quality_table or data_quality.TABLE
		with pymysql.connect(**config) as conn:
			data_quality.store(conn, document, table, args.create_quality_table)
		# the table's name only: where the database is stays on the migration side
		destinations.append("table " + table)
	receipt = call(
		"quality_received",
		run=args.run,
		sha256=document["sha256"],
		rows=len(rows),
		destination=", ".join(destinations),
	)
	failed = sum(row["passed"] is False for row in rows)
	return {"run": args.run, "state": document["state"], "metrics": len(rows), "failed": failed, **receipt}


def main():
	"""`ucm-push`. The short commands and the settings file are in `publishing`, which completes a command and
	hands it to `run`."""
	from . import publishing

	publishing.main()


def run(argv: list[str] | None = None, readable: bool = False):
	"""One detailed command, every value an option. `readable`: answer in sentences where the command has some."""
	from .publishing import show

	parser = argparse.ArgumentParser(
		prog="ucm-push",
		description=__doc__,
		epilog="Short commands: ucm-push init writes the host's files to fill in; then ucm-push seal CODE, and "
		"send, status and quality from the publication's folder. ucm-push --help-settings describes the "
		"settings file.",
	)
	aliases = {"activity-order": "upload-order", "freeze": "prepare"}
	parser.add_argument(
		"action",
		type=lambda value: aliases.get(value, value),
		choices=[
			"plan",
			"assess",
			"source-inventory",
			"upload-order",
			"prepare",
			"check-publication",
			"locate-item",
			"propose",
			"send-publication",
			"transfer-status",
			"push",
			"status",
			"report",
			"reconcile",
			"quality",
			"verify",
			"apply",
			"pause",
			"activate",
			"discard",
		],
	)
	parser.add_argument(
		"--source-config",
		type=Path,
		help="Private PyMySQL connection JSON for the publisher",
	)
	parser.add_argument("--profile", choices=PROFILES, default=PROFILE)
	parser.add_argument("--site", required=True)
	parser.add_argument("--snapshot")
	parser.add_argument(
		"--ucm-version",
		help="Source UCM release for publishing databases without installed application metadata",
	)
	parser.add_argument("--publication-out", type=Path)
	parser.add_argument(
		"--findings-out",
		type=Path,
		help="Write private JSON Lines for activities missing required places when preparation is refused",
	)
	parser.add_argument("--publication", type=Path)
	parser.add_argument("--handoff", type=Path, help="The hand-off of a sealed publication, for propose")
	parser.add_argument("--sequence", type=int, help="Recorded batch number")
	parser.add_argument("--item", type=int, help="One-based position inside the batch")
	parser.add_argument("--private-files", type=Path)
	parser.add_argument("--attempt", type=int, default=1, help="Increment for a fresh run after discard")
	parser.add_argument("--context")
	parser.add_argument("--url", help="Target HTTPS base URL")
	parser.add_argument("--run")
	parser.add_argument("--manifest-out", type=Path, help="Write the plan for target-side approval")
	parser.add_argument(
		"--max-batches",
		type=int,
		help="Upload or apply at most this many new batches in this invocation",
	)
	parser.add_argument(
		"--progress",
		action="store_true",
		help="Print sealed transfer byte checkpoints on stderr",
	)
	parser.add_argument("--batch-size", type=int, default=100)
	parser.add_argument("--batch-bytes", type=int, default=MAX_BYTES)
	parser.add_argument("--expected", type=Path, help="Independently reviewed pilot rows for reconcile")
	parser.add_argument(
		"--quality-out",
		type=Path,
		help="Write the run's data quality metrics to this file (.csv, or JSON)",
	)
	parser.add_argument(
		"--quality-config",
		type=Path,
		help="Private PyMySQL connection JSON of the data quality database",
	)
	parser.add_argument("--quality-table", default=None, help="Metrics table (default ucm_target_metric)")
	parser.add_argument(
		"--create-quality-table",
		action="store_true",
		help="Create the metrics table when it is absent",
	)
	args = parser.parse_args(argv)
	if (args.action == "quality") != bool(args.quality_out or args.quality_config):
		parser.error("Use --quality-out or --quality-config with quality")
	if (args.quality_table or args.create_quality_table) and not args.quality_config:
		parser.error("--quality-table and --create-quality-table require --quality-config")
	if args.ucm_version and args.action != "prepare":
		parser.error("--ucm-version requires prepare")
	if args.findings_out and args.action != "prepare":
		parser.error("--findings-out requires prepare")
	if bool(args.publication_out) != (args.action == "prepare") or (
		args.private_files and args.action != "prepare"
	):
		parser.error("Supply --publication-out for prepare and --private-files when attachments are present")
	if (args.action == "reconcile") != bool(args.expected):
		parser.error("Use --expected with reconcile")

	if args.max_batches is not None and (
		args.action not in ("push", "apply", "send-publication") or args.max_batches < 1
	):
		parser.error("--max-batches requires push or apply and a positive limit")
	if args.manifest_out and args.action != "plan":
		parser.error("--manifest-out requires plan")
	if args.action == "locate-item":
		from .frozen import locate_item

		if not args.publication or not args.run or not args.sequence or not args.item:
			parser.error("Supply --publication, --run, --sequence and --item")
		print(json.dumps(locate_item(args.publication, args.run, args.site, args.sequence, args.item)))
		return
	if args.sequence is not None or args.item is not None:
		parser.error("--sequence and --item require locate-item")
	if args.action == "check-publication":
		from .frozen import verify_publication

		if not args.run or not args.publication:
			parser.error("Supply --publication and the retained --run")
		manifest = verify_publication(args.publication, args.run)
		if manifest["site"] != args.site:
			raise ValueError("Publication targets another site")
		with args.publication.open("rb") as file:
			fingerprint = hashlib.file_digest(file, "sha256").hexdigest()
			size = os.fstat(file.fileno()).st_size
		print(
			json.dumps(
				{
					"run": args.run,
					"verified": True,
					"file_sha256": fingerprint,
					"file_bytes": size,
					**manifest,
				}
			)
		)
		return
	session = requests.Session()
	session.mount(
		"https://",
		HTTPAdapter(
			max_retries=Retry(
				total=3,
				backoff_factor=0.5,
				allowed_methods={"POST"},
				status_forcelist=(429, 502, 503, 504),
				raise_on_status=False,
			)
		),
	)

	def call(method, **data):
		if not args.url or urlsplit(args.url).scheme != "https":
			parser.error("Set --url to the target HTTPS base URL")
		module = "publication_transport" if args.action in ("send-publication", "transfer-status") else "api"
		response = session.post(
			f"{args.url.rstrip('/')}/api/method/ucm_migration_loader.{module}.{method}",
			json=data,
			headers={"Authorization": "token " + os.environ["UCM_MIGRATION_TOKEN"]},
			timeout=(8, 45),
			allow_redirects=False,
		)
		if response.status_code != 200:
			raise SystemExit(f"{method} returned HTTP {response.status_code}. Check the run and target log.")
		return response.json()["message"]

	if args.action == "transfer-status":
		if not args.run:
			parser.error("Supply the approved --run")
		receipt = call("status", run=args.run)
		if (receipt["site"], receipt["run"]) != (args.site, args.run):
			raise ValueError("Target transfer approval differs")
		show(args.action, receipt, readable)
		return

	if args.action == "send-publication":
		import base64

		from .frozen import verify_publication

		if not args.run or not args.publication:
			parser.error("Supply --publication and the retained --run")
		if verify_publication(args.publication, args.run)["site"] != args.site:
			raise ValueError("Publication targets another site")
		receipt = call("status", run=args.run)
		with args.publication.open("rb") as publication_file:
			checksum = hashlib.file_digest(publication_file, "sha256").hexdigest()
			size = os.fstat(publication_file.fileno()).st_size
			if (
				receipt["sha256"],
				receipt["size"],
				receipt["site"],
				receipt["run"],
			) != (checksum, size, args.site, args.run):
				raise ValueError("Target approval differs from the publication")
			offset = receipt["offset"]
			chunk_size = 512 * 1024
			if not 0 <= offset <= size or (offset != size and offset % chunk_size):
				raise ValueError("Invalid transfer checkpoint")
			publication_file.seek(offset)
			count = 0
			while offset < size and (args.max_batches is None or count < args.max_batches):
				chunk = publication_file.read(chunk_size)
				receipt = call(
					"upload",
					run=args.run,
					offset=offset,
					data=base64.b64encode(chunk).decode("ascii"),
				)
				next_offset = receipt["offset"]
				if (
					(
						receipt["run"],
						receipt["site"],
						receipt["sha256"],
						receipt["size"],
					)
					!= (args.run, args.site, checksum, size)
					or not offset + len(chunk) <= next_offset <= size
					or (next_offset != size and next_offset % chunk_size)
				):
					raise ValueError("Unexpected transfer checkpoint")
				# A second authorized sender may have already completed later chunks.
				offset = next_offset
				publication_file.seek(offset)
				if args.progress or sys.stderr.isatty():
					print(
						f"Transferred {offset:,} of {size:,} bytes ({offset / size:.1%})",
						file=sys.stderr,
						flush=True,
					)
				count += 1
		show(args.action, receipt, readable)
		return

	if args.action in (
		"assess",
		"source-inventory",
		"upload-order",
		"prepare",
		"plan",
		"push",
	):
		if not all((args.source_config, args.context, args.snapshot)):
			parser.error("Supply --source-config, --context and --snapshot")
		with pymysql.connect(**json.loads(args.source_config.read_text())) as conn:
			with conn.cursor() as cur:
				cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
				cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")

			if args.action == "source-inventory":
				from .source_inventory import inventory

				print(
					json.dumps(
						{
							"site": args.site,
							"snapshot": args.snapshot,
							**inventory(conn, args.context),
						}
					)
				)
				return
			if args.action == "prepare":
				from .frozen import freeze

				print(
					json.dumps(
						freeze(
							conn,
							args.publication_out,
							args.private_files,
							args.site,
							args.snapshot,
							args.context,
							args.batch_size,
							args.batch_bytes,
							args.findings_out,
							ucm_version=args.ucm_version,
						)
					)
				)
				return
			if args.action == "upload-order":
				from .activity_order import order_report

				order = order_report(conn, args.context, args.batch_size)
				print(
					json.dumps(
						{
							"site": args.site,
							"context": args.context,
							"snapshot": args.snapshot,
							**order,
						}
					)
				)
				return
			scope = assess(conn, args.context, args.profile)
			if args.action == "assess":
				print(
					json.dumps(
						{
							"site": args.site,
							"context": args.context,
							"snapshot": args.snapshot,
							**scope,
						}
					)
				)
				return
			if not scope["eligible_for_push"] and (args.action == "push" or args.manifest_out):
				print(
					json.dumps(
						{
							"site": args.site,
							"context": args.context,
							"snapshot": args.snapshot,
							**scope,
						}
					)
				)
				raise SystemExit(1)
			schema = publisher_schema(conn, args.profile)
			manifest, batches = plan(
				args.site,
				args.snapshot,
				args.context,
				publication(conn, args.context, args.profile),
				args.batch_size,
				args.batch_bytes,
				args.attempt,
				args.profile,
			)
		manifest["schema"] = schema
		result = {
			**scope,
			"run": digest(manifest),
			"batches": len(batches),
			"documents": sum(len(batch) for batch in batches),
			"payload_bytes": sum(len(encoded(batch)) for batch in batches),
			"rows_by_type": {
				**{
					dt: sum(doc["doctype"] == dt for batch in batches for doc in batch)
					for dt in PROFILES[args.profile]
				},
				"Person Other Name": sum(
					len(doc.get("person_other_names", [])) for batch in batches for doc in batch
				),
			},
		}
		if args.manifest_out:
			args.manifest_out.write_text(json.dumps(manifest, indent=2) + "\n")
		if args.action == "push":
			result = send(call, manifest, batches, args.max_batches)
	elif args.action == "reconcile":
		from .evidence import compare_expected

		if not args.run:
			parser.error("Supply --run")
		target = call("report", run=args.run)
		if target["run"] != args.run:
			raise ValueError("Target returned a different run")
		expected = json.loads(args.expected.read_text())
		if expected.get("site") != args.site:
			raise ValueError("Expected rows target a different site")
		result = compare_expected(expected, target)
		if not result["matched"]:
			print(json.dumps(result))
			raise SystemExit(1)
	elif args.action == "quality":
		if not args.run:
			parser.error("Supply --run")
		result = hand_over_quality(call, args)
	elif args.action == "propose":
		if not args.handoff:
			parser.error("Supply --handoff")
		handoff = json.loads(args.handoff.read_text())
		if handoff.get("site") != args.site:
			raise ValueError("The hand-off is for another site")
		result = {**call("propose", handoff=handoff), "check_code": check_code(handoff["file_sha256"])}
	else:
		if not args.run:
			parser.error("Supply --run")
		options = {"max_batches": args.max_batches} if args.action == "apply" and args.max_batches else {}
		result = call(args.action, run=args.run, **options)
	show(args.action, result, readable)


if __name__ == "__main__":
	main()
