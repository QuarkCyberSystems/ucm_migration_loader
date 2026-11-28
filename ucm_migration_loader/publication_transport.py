"""Bounded HTTPS transfer for a publication whose byte digest was approved independently."""

import base64
import errno
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

from . import constants, runs
from .contract import TRANSFER_CHUNK as CHUNK
from .publication_receiver import arriving, local_publication, stage
from .publication_receiver import inbox as folder

# left in a run's inbox once its file is staged
STAGED = "staged"
# in a run's inbox while its file arrives: the bytes of it that are safely on the disk
OFFSET = "offset"


def approve(run: str, publisher: str, sha256: str, size: int):
	runs.only_admin()
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
	"""How far the run's file has arrived: what the site wrote to the disk and noted, piece after piece."""
	size = approval["size"]
	if (root / STAGED).is_file():
		offset = size  # the whole file arrived and was staged
	else:
		noted = root / OFFSET
		offset = min(int(noted.read_text()), size) if noted.is_file() else 0
	return {
		"run": approval["run"],
		"site": approval["site"],
		"size": size,
		"sha256": approval["sha256"],
		"offset": offset,
	}


@frappe.whitelist(methods=["POST"])
def status(run: str):
	with transfer(run) as (root, approval):
		return progress(root, approval)


def _synced(folder: Path) -> None:
	fd = os.open(folder, os.O_RDONLY)
	try:
		os.fsync(fd)
	finally:
		os.close(fd)


def _append(run: str, root: Path, offset: int, value: bytes) -> None:
	"""Write one piece at the end of the arriving file and note the new end, each safely on the disk before the
	sender is answered. What the file holds beyond the noted end was never acknowledged (a piece cut short by
	a crash): it is dropped first."""
	file = arriving(run)
	new = not file.exists()
	fd = os.open(file, os.O_WRONLY | os.O_CREAT, 0o600)
	try:
		os.ftruncate(fd, offset)
		written = 0
		while written < len(value):
			written += os.pwrite(fd, value[written:], offset + written)
		os.fsync(fd)
	except OSError as failed:
		if failed.errno in (errno.ENOSPC, errno.EDQUOT):
			raise ValueError("The site's disk is full: free space on the site, then send again") from failed
		raise
	finally:
		os.close(fd)
	if new:
		_synced(file.parent)
	with tempfile.TemporaryDirectory(dir=root) as work:
		noted = Path(work) / OFFSET
		with noted.open("x") as out:
			os.fchmod(out.fileno(), 0o600)
			out.write(str(offset + len(value)))
			out.flush()
			os.fsync(out.fileno())
		os.replace(noted, root / OFFSET)
	_synced(root)


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
		if (root / STAGED).is_file():
			return before  # the file is staged, and no longer here to compare a piece with
		if offset < before["offset"]:
			# a piece that arrived before, sent again by a sender who never heard the answer
			with arriving(run).open("rb") as file:
				file.seek(offset)
				if file.read(expected) != value:
					raise ValueError("Replayed chunk differs")
		else:
			_append(run, root, offset, value)
		arrived = progress(root, approval)
		if arrived["offset"] == approval["size"]:
			_stage_when_committed(run)
		return arrived


def _stage_when_committed(run: str) -> None:
	"""The whole file is here: have the migration queue stage it. The approval was the decision; staging checks
	the file against it and writes nothing into UCM."""
	if frappe.db.get_value(constants.RUN, run, "state") != constants.AWAITING:
		return  # approved from the bench without a run, or staged already: `receive` stages it by hand
	frappe.enqueue(
		"ucm_migration_loader.publication_transport.stage_arrived",
		queue=constants.QUEUE,
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
	record = frappe.db.get_value(constants.RUN, run, ["state", "approval_json"], as_dict=True)
	approver = json.loads((record and record.approval_json) or "{}").get("by")
	if not record or record.state != constants.AWAITING or not approver:
		return
	# nosemgrep: frappe-semgrep-rules.rules.security.frappe-setuser -- reviewed: the job is queued by the sender's last piece and must not run with the sender's rights or with more than the approver's; receive() checks the role again and compares the file with the approved fingerprint
	frappe.set_user(approver)
	try:
		receive(run)
	except Exception as failed:
		frappe.db.rollback()
		if frappe.db.get_value(constants.RUN, run, "state") == constants.REJECTED:
			return  # refused by the site's checks: the run says what differs
		# the loader's own refusals are sentences without data; another error gives its kind only
		reason = str(failed) if isinstance(failed, ValueError) else type(failed).__name__
		frappe.db.set_value(constants.RUN, run, "finding", f"Staging failed: {reason}"[:140])
		# nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit -- reviewed: the failure is kept on the run although the job fails
		frappe.db.commit()
		raise


def mark_staged(run: str) -> None:
	"""The run's file is staged, under its staged name: leave a mark, so that a sender who asks again is told
	the whole file arrived."""
	root = folder(run)
	(root / STAGED).touch(mode=0o600)
	(root / OFFSET).unlink(missing_ok=True)


def receive(run: str):
	"""Stage the run's arrived file: compare it, where it arrived, with the approved size and fingerprint, then
	let the site's checks decide and rename it to its staged name. No copy of it is made."""
	runs.only_admin()
	root = folder(run)
	if (root / STAGED).is_file():
		return runs.result(runs.record(run))
	approval = json.loads((root / "approval.json").read_bytes())
	if progress(root, approval)["offset"] != approval["size"]:
		raise ValueError("Publication transfer is incomplete")
	# where a staging renamed the file and died before its commit, the file waits under its staged name
	path = arriving(run) if arriving(run).is_file() else local_publication(run)
	digest = hashlib.sha256()
	with path.open("rb") as file:
		while block := file.read(1024 * 1024):
			digest.update(block)
	if path.stat().st_size != approval["size"] or digest.hexdigest() != approval["sha256"]:
		raise ValueError("Transfer differs from the approved file")
	result = stage(str(path), run, take=True)
	# the account that sent the file may read the status, the report and the metrics of this
	# run, and of no other (runs.sender_or_admin)
	frappe.db.set_value(constants.RUN, run, "publishing_user", approval["publisher"], update_modified=False)
	# once the staging is committed; a staging that is rolled back is asked for again
	frappe.db.after_commit.add(lambda: mark_staged(run))
	return result
