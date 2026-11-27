"""Bench entry point for registering the manifest reviewed by a target operator."""

import json
from pathlib import Path

from . import api


def prepare_publication(manifest_path: str, publishing_user: str):
	api._operator()
	if not publishing_user:
		raise ValueError("Select the publishing account for this manifest")
	return api.begin(json.loads(Path(manifest_path).read_text()), publishing_user)


def approve_sealed(handoff_path: str, publishing_user: str):
	"""Register the independently delivered check-publication report on the named site."""
	import frappe

	from .contract import digest
	from .frozen import FORMAT
	from .publication_transport import approve

	api._operator()
	handoff = json.loads(Path(handoff_path).read_text())
	run = handoff.pop("run")
	verified = handoff.pop("verified")
	sha = handoff.pop("file_sha256")
	size = handoff.pop("file_bytes")
	if (
		verified is not True
		or handoff.get("site") != frappe.local.site
		or handoff.get("format") != FORMAT
		or digest(handoff) != run
		or "Organization" not in handoff.get("counts", {})
		or "sensitive_field_policy" not in handoff
	):
		raise ValueError("Handoff does not match the reviewed full publication and target site")
	return approve(run, publishing_user, sha, size)


def run_of(handoff_or_run: str) -> str:
	"""The run a site operator names: by the hand-off file they were given, or by the run's identifier."""
	path = Path(handoff_or_run)
	if path.is_file():
		return json.loads(path.read_text())["run"]
	return handoff_or_run


def approve_handoff(handoff_path: str, publishing_user: str) -> dict:
	"""Approve a reviewed hand-off for one publishing account, and say what was approved."""
	handoff = json.loads(Path(handoff_path).read_text())
	approval = approve_sealed(handoff_path, publishing_user)
	return {
		"run": approval["run"],
		"publisher": approval["publisher"],
		"rows": sum(handoff["counts"].values()),
		"batches": handoff["batches"],
		"bytes": approval["size"],
		"snapshot": handoff.get("snapshot"),
	}


def stage_arrived(handoff_or_run: str) -> dict:
	"""Stage the file of an approved run once it has arrived, and say how it went: staged, refused by the
	site's checks (the refused run is kept), not arrived completely, or never approved here."""
	import frappe

	from .publication_transport import folder, progress, receive

	api._operator()
	run = run_of(handoff_or_run)
	approval = folder(run) / "approval.json"
	if not approval.is_file():
		return {"outcome": "not approved", "run": run}
	arrived = progress(folder(run), json.loads(approval.read_bytes()))
	if arrived["offset"] != arrived["size"]:
		return {"outcome": "incomplete", "run": run, "arrived": arrived["offset"], "bytes": arrived["size"]}
	try:
		result = receive(run)
	except ValueError as refused:
		frappe.db.rollback()
		kept = frappe.db.get_value(api.RUN, run, ["display_id", "state", "finding"], as_dict=True)
		if not kept or kept.state != "Rejected" or "preflight failed" not in str(refused):
			raise
		return {"outcome": "refused", "run": run, "display_id": kept.display_id, "finding": kept.finding}
	record = frappe.db.get_value(api.RUN, run, ["display_id", "document_count", "received"], as_dict=True)
	return {
		"outcome": "staged",
		"run": run,
		"display_id": record.display_id,
		"state": result["state"],
		"rows": record.document_count,
		"batches": record.received,
	}
