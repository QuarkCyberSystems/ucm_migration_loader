"""Seal one delegation of a publishing database into a file, and take it to a UCM site."""

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pymysql
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .contract import (
	MAX_PUBLICATION_BATCH_BYTES,
	MAX_PUBLICATION_BATCH_DOCUMENTS,
	TRANSFER_CHUNK,
	check_code,
	digest,
)

ALIASES = {"activity-order": "upload-order", "freeze": "prepare"}
# the options a command cannot do without, and what it says where one is missing
SOURCE = (("source_config", "context", "snapshot"), "Supply --source-config, --context and --snapshot")
SEALED = (("run", "publication"), "Supply --publication and the retained --run")
ON_A_RUN = (("run",), "Supply --run")
REQUIRED = {
	"source-inventory": SOURCE,
	"upload-order": SOURCE,
	"prepare": SOURCE,
	"check-publication": SEALED,
	"locate-item": (
		("publication", "run", "sequence", "item"),
		"Supply --publication, --run, --sequence and --item",
	),
	"propose": (("handoff",), "Supply --handoff"),
	"send-publication": SEALED,
	"transfer-status": (("run",), "Supply the approved --run"),
	"status": ON_A_RUN,
	"report": ON_A_RUN,
	"quality": ON_A_RUN,
	"apply": ON_A_RUN,
	"pause": ON_A_RUN,
	"activate": ON_A_RUN,
	"discard": ON_A_RUN,
}


def _said(response) -> str:
	"""What the site said with a refusal, where it said something: its message, without markup."""
	try:
		messages = json.loads(response.json().get("_server_messages") or "[]")
		text = json.loads(messages[-1]).get("message") if messages else ""
	except (ValueError, AttributeError, TypeError, IndexError):
		return ""
	text = re.sub(r"<[^>]+>", "", str(text or "")).strip().rstrip(".")
	return f": {text}" if text else ""


class Site:
	"""The receiving site as the publishing account reaches it: its methods over HTTPS, with the account's
	token."""

	def __init__(self, url: str):
		self.url = url.rstrip("/")
		self.session = requests.Session()
		self.session.mount(
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

	def _post(self, module: str, method: str, data: dict):
		response = self.session.post(
			f"{self.url}/api/method/ucm_migration_loader.{module}.{method}",
			json=data,
			headers={"Authorization": "token " + os.environ["UCM_MIGRATION_TOKEN"]},
			timeout=(8, 45),
			allow_redirects=False,
		)
		if response.status_code != 200:
			raise SystemExit(
				f"{method} returned HTTP {response.status_code}{_said(response)}. Check the run and target log."
			)
		return response.json()["message"]

	def api(self, method: str, **data):
		"""A run's status, report, metrics and actions."""
		return self._post("api", method, data)

	def transfer(self, method: str, **data):
		"""The transfer of an approved run's sealed file."""
		return self._post("publication_transport", method, data)


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


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		prog="ucm-push",
		description=__doc__,
		epilog="Short commands: ucm-push init writes the host's files to fill in; then ucm-push seal CODE, and "
		"send, status and quality from the publication's folder. ucm-push --help-settings describes the "
		"settings file.",
	)
	parser.add_argument(
		"action",
		type=lambda value: ALIASES.get(value, value),
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
	parser.add_argument("--batch-size", type=int, default=MAX_PUBLICATION_BATCH_DOCUMENTS)
	parser.add_argument("--batch-bytes", type=int, default=MAX_PUBLICATION_BATCH_BYTES)
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
	return parser


def check_options(parser: argparse.ArgumentParser, args) -> None:
	"""Refuse a command whose options do not fit it, before it does anything."""
	from .publishing import LOCAL

	action = args.action
	if (action == "quality") != bool(args.quality_out or args.quality_config):
		parser.error("Use --quality-out or --quality-config with quality")
	if (args.quality_table or args.create_quality_table) and not args.quality_config:
		parser.error("--quality-table and --create-quality-table require --quality-config")
	if args.ucm_version and action != "prepare":
		parser.error("--ucm-version requires prepare")
	if args.findings_out and action != "prepare":
		parser.error("--findings-out requires prepare")
	if bool(args.publication_out) != (action == "prepare") or (args.private_files and action != "prepare"):
		parser.error("Supply --publication-out for prepare and --private-files when attachments are present")
	if args.max_batches is not None and (action not in ("apply", "send-publication") or args.max_batches < 1):
		parser.error("--max-batches requires apply or send-publication and a positive limit")
	if action != "locate-item" and (args.sequence is not None or args.item is not None):
		parser.error("--sequence and --item require locate-item")
	options, message = REQUIRED[action]
	if not all(getattr(args, option) for option in options):
		parser.error(message)
	if action not in LOCAL:
		# before any work: a transfer verifies the whole file first, and would fail only afterwards
		if not args.url or urlsplit(args.url).scheme != "https":
			parser.error("Set --url to the target HTTPS base URL")
		if not os.environ.get("UCM_MIGRATION_TOKEN"):
			parser.error(
				"Set UCM_MIGRATION_TOKEN to the publishing account's key:secret, or name its file as "
				"token_file in the settings"
			)


# ---------------------------------------------------------------------------------------------------------------
# on the publishing host alone


def _source_inventory(args, site):
	from .frozen import source_snapshot
	from .source_inventory import inventory

	with source_snapshot(args.source_config) as source:
		return {"site": args.site, "snapshot": args.snapshot, **inventory(source, args.context)}


def _upload_order(args, site):
	from .activity_order import order_report
	from .frozen import source_snapshot

	with source_snapshot(args.source_config) as source:
		order = order_report(source, args.context, args.batch_size)
	return {"site": args.site, "context": args.context, "snapshot": args.snapshot, **order}


def _prepare(args, site):
	from .frozen import freeze, source_snapshot

	with source_snapshot(args.source_config) as source:
		return freeze(
			source,
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


def _for_this_site(args) -> dict:
	"""The manifest of the sealed file named, checked whole; refused where it was sealed for another site."""
	from .frozen import verify_publication

	manifest = verify_publication(args.publication, args.run)
	if manifest["site"] != args.site:
		raise ValueError("Publication targets another site")
	return manifest


def _check_publication(args, site):
	from .frozen import hand_off

	return hand_off(args.publication, args.run, _for_this_site(args))


def _locate_item(args, site):
	from .frozen import locate_item

	return locate_item(args.publication, args.run, args.site, args.sequence, args.item)


# ---------------------------------------------------------------------------------------------------------------
# with the site


def _propose(args, site):
	handoff = json.loads(args.handoff.read_text())
	if handoff.get("site") != args.site:
		raise ValueError("The hand-off is for another site")
	return {**site.api("propose", handoff=handoff), "check_code": check_code(handoff["file_sha256"])}


def _transfer_status(args, site):
	receipt = site.transfer("status", run=args.run)
	if (receipt["site"], receipt["run"]) != (args.site, args.run):
		raise ValueError("Target transfer approval differs")
	return receipt


def _send_publication(args, site):
	"""Send the sealed file from where the site has it, piece by piece. Each answer is checked against the file
	in hand: the site, the run, the fingerprint and the size it was approved with."""
	from .frozen import file_identity

	_for_this_site(args)
	checksum, size = file_identity(args.publication)
	approved = (args.run, args.site, checksum, size)

	def checked(receipt, least):
		if (receipt["run"], receipt["site"], receipt["sha256"], receipt["size"]) != approved:
			raise ValueError("Target approval differs from the publication")
		offset = receipt["offset"]
		if not least <= offset <= size or (offset != size and offset % TRANSFER_CHUNK):
			raise ValueError("Unexpected transfer checkpoint")
		return offset

	receipt = site.transfer("status", run=args.run)
	offset = checked(receipt, 0)
	sent = 0
	with args.publication.open("rb") as publication_file:
		while offset < size and (args.max_batches is None or sent < args.max_batches):
			publication_file.seek(offset)
			piece = publication_file.read(TRANSFER_CHUNK)
			receipt = site.transfer(
				"upload", run=args.run, offset=offset, data=base64.b64encode(piece).decode("ascii")
			)
			# another sender of the same account may have completed later pieces already
			offset = checked(receipt, offset + len(piece))
			sent += 1
			if args.progress or sys.stderr.isatty():
				print(
					f"Transferred {offset:,} of {size:,} bytes ({offset / size:.1%})",
					file=sys.stderr,
					flush=True,
				)
	return receipt


def _quality(args, site):
	return hand_over_quality(site.api, args)


def _on_a_run(args, site):
	"""status, report and the actions of a Migration Admin: the site's method of the same name."""
	options = {"max_batches": args.max_batches} if args.action == "apply" and args.max_batches else {}
	return site.api(args.action, run=args.run, **options)


ACTIONS = {
	"source-inventory": _source_inventory,
	"upload-order": _upload_order,
	"prepare": _prepare,
	"check-publication": _check_publication,
	"locate-item": _locate_item,
	"propose": _propose,
	"send-publication": _send_publication,
	"transfer-status": _transfer_status,
	"quality": _quality,
	"status": _on_a_run,
	"report": _on_a_run,
	"apply": _on_a_run,
	"pause": _on_a_run,
	"activate": _on_a_run,
	"discard": _on_a_run,
}


def run(argv: list[str] | None = None, readable: bool = False):
	"""One detailed command, every value an option. `readable`: answer in sentences where the command has some."""
	from .publishing import LOCAL, show

	parser = build_parser()
	args = parser.parse_args(argv)
	check_options(parser, args)
	site = None if args.action in LOCAL else Site(args.url)
	show(args.action, ACTIONS[args.action](args, site), readable)


if __name__ == "__main__":
	main()
