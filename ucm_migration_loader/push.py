"""Seal one delegation of a publishing database into a file, and take it to a UCM site."""

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

from .contract import check_code, digest


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
			"source-inventory",
			"upload-order",
			"prepare",
			"check-publication",
			"locate-item",
			"propose",
			"send-publication",
			"transfer-status",
			"status",
			"report",
			"quality",
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
	parser.add_argument("--context")
	parser.add_argument("--url", help="Target HTTPS base URL")
	parser.add_argument("--run")
	parser.add_argument(
		"--max-batches",
		type=int,
		help="Apply at most this many new batches, or send at most this many pieces, in this invocation",
	)
	parser.add_argument(
		"--progress",
		action="store_true",
		help="Print sealed transfer byte checkpoints on stderr",
	)
	parser.add_argument("--batch-size", type=int, default=100)
	parser.add_argument("--batch-bytes", type=int, default=256 * 1024)
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
	if args.max_batches is not None and (
		args.action not in ("apply", "send-publication") or args.max_batches < 1
	):
		parser.error("--max-batches requires apply or send-publication and a positive limit")
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

	if args.action in ("source-inventory", "upload-order", "prepare"):
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
	if args.action == "quality":
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
