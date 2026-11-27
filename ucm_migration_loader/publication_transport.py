"""Bounded HTTPS transfer for a publication whose byte digest was approved independently."""

import base64
import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

import frappe
from frappe import _

from . import api
from .publication_receiver import local_publication, stage

CHUNK = 512 * 1024


def folder(run):
	local_publication(run)  # validate identifier before constructing any path
	return Path(frappe.get_site_path("private", "migration-inbox", run))


def approve(run: str, publisher: str, sha256: str, size: int):
	api._operator()
	if not re.fullmatch(r"[a-f0-9]{64}", sha256) or int(size) < 1:
		frappe.throw(_("Invalid approved publication fingerprint"))
	if publisher in ("Guest", "Administrator") or not frappe.db.get_value("User", publisher, "enabled"):
		frappe.throw(_("Select an enabled publishing account"))
	root = folder(run)
	root.mkdir(mode=0o700, parents=True, exist_ok=True)
	approval = {
		"run": run,
		"publisher": publisher,
		"sha256": sha256,
		"size": int(size),
		"site": frappe.local.site,
	}
	with tempfile.TemporaryDirectory(dir=root) as work:
		temporary = Path(work) / "approval"
		with temporary.open("xb") as out:
			os.fchmod(out.fileno(), 0o600)
			out.write(json.dumps(approval).encode())
			out.flush()
			os.fsync(out.fileno())
		dest = root / "approval.json"
		try:
			os.link(temporary, dest)
		except FileExistsError:
			if json.loads(dest.read_bytes()) != approval:
				frappe.throw(_("This run was approved before, for another account or another file"))
	return approval


@contextmanager
def transfer(run):
	root = folder(run)
	approval_file = root / "approval.json"
	if not approval_file.is_file():
		frappe.throw(_("Publication transfer unavailable"), frappe.PermissionError)
	approval = json.loads(approval_file.read_bytes())
	if approval["publisher"] != frappe.session.user:
		frappe.throw(_("Publication transfer unavailable"), frappe.PermissionError)
	with approval_file.open("rb") as lock:
		fcntl.flock(lock, fcntl.LOCK_EX)
		yield root, approval


def progress(root, approval):
	# Each completed chunk is published by an atomic hard link after its fsync.
	# Use binary search over a contiguous immutable prefix to recover the cursor.
	size = approval["size"]
	lo, hi = 0, (size + CHUNK - 1) // CHUNK
	while lo < hi:
		mid = (lo + hi + 1) // 2
		if (root / str(mid - 1)).is_file():
			lo = mid
		else:
			hi = mid - 1
	return {
		"run": approval["run"],
		"site": approval["site"],
		"size": size,
		"sha256": approval["sha256"],
		"offset": min(lo * CHUNK, size),
	}


@frappe.whitelist(methods=["POST"])
def status(run: str):
	with transfer(run) as (root, approval):
		return progress(root, approval)


@frappe.whitelist(methods=["POST"])
def upload(run: str, offset: int, data: str):
	with transfer(run) as (root, approval):
		before = progress(root, approval)
		if not 0 <= offset <= before["offset"] or offset >= approval["size"] or offset % CHUNK:
			raise ValueError("Unexpected publication offset")
		expected = min(CHUNK, approval["size"] - offset)
		if len(data) != 4 * ((expected + 2) // 3):
			raise ValueError("Unexpected encoded chunk length")
		value = base64.b64decode(data, validate=True)
		if len(value) != expected:
			raise ValueError("Unexpected chunk length")
		dest = root / str(offset // CHUNK)
		if dest.exists():
			if dest.read_bytes() != value:
				raise ValueError("Replayed chunk differs")
		else:
			with tempfile.TemporaryDirectory(dir=root) as work:
				temp = Path(work) / "part"
				with temp.open("xb") as out:
					os.fchmod(out.fileno(), 0o600)
					out.write(value)
					out.flush()
					os.fsync(out.fileno())
				os.link(temp, dest)
				fd = os.open(root, os.O_RDONLY)
				try:
					os.fsync(fd)
				finally:
					os.close(fd)
		arrived = progress(root, approval)
		if arrived["offset"] == approval["size"]:
			_stage_when_committed(run)
		return arrived


def _stage_when_committed(run: str) -> None:
	"""The whole file is here: have the migration queue stage it. The approval was the decision; staging checks
	the file against it and writes nothing into UCM."""
	if frappe.db.get_value(api.RUN, run, "state") != api.AWAITING:
		return  # approved from the bench without a run, or staged already: `receive` stages it by hand
	frappe.enqueue(
		"ucm_migration_loader.publication_transport.stage_arrived",
		queue=api.QUEUE,
		timeout=3600,
		job_id=f"ucm_migration_stage::{run}",
		deduplicate=True,
		enqueue_after_commit=True,
		run=run,
	)


def stage_arrived(run: str) -> None:
	"""Queued job: stage the file of a run that waits for it, in the name of the Migration Admin who approved
	it. A file the site's checks refuse is kept as a refused run; any other failure is noted on the run, which
	keeps waiting until someone stages it from the Desk."""
	record = frappe.db.get_value(api.RUN, run, ["state", "approval_json"], as_dict=True)
	approver = json.loads((record and record.approval_json) or "{}").get("by")
	if not record or record.state != api.AWAITING or not approver:
		return
	# nosemgrep: frappe-semgrep-rules.rules.security.frappe-setuser -- reviewed: the job is queued by the sender's last piece and must not run with the sender's rights or with more than the approver's; receive() checks the role again and compares the file with the approved fingerprint
	frappe.set_user(approver)
	try:
		receive(run)
	except Exception as failed:
		frappe.db.rollback()
		if frappe.db.get_value(api.RUN, run, "state") == "Rejected":
			return  # refused by the site's checks: the run says what differs
		# the loader's own refusals are sentences without data; another error gives its kind only
		reason = str(failed) if isinstance(failed, ValueError) else type(failed).__name__
		frappe.db.set_value(api.RUN, run, "finding", f"Staging failed: {reason}"[:140])
		# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: the failure is kept on the run although the job fails
		frappe.db.commit()
		raise


def receive(run: str):
	api._operator()
	root = folder(run)
	approval = json.loads((root / "approval.json").read_bytes())
	if progress(root, approval)["offset"] != approval["size"]:
		raise ValueError("Publication transfer is incomplete")
	digest = hashlib.sha256()
	with tempfile.TemporaryDirectory(dir=root) as work:
		path = Path(work) / "sealed"
		with path.open("xb") as out:
			for offset in range(0, approval["size"], CHUNK):
				data = (root / str(offset // CHUNK)).read_bytes()
				digest.update(data)
				out.write(data)
		if path.stat().st_size != approval["size"] or digest.hexdigest() != approval["sha256"]:
			raise ValueError("Transfer differs from the approved file")
		result = stage(str(path), run)
		# the account that sent the file may read the status, the report and the metrics of this
		# run, and of no other (api._publisher_or_operator)
		frappe.db.set_value(api.RUN, run, "publishing_user", approval["publisher"], update_modified=False)
		return result
