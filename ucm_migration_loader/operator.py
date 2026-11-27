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
