"""Bench entry point for registering the manifest reviewed by a target operator."""

import json
from pathlib import Path

from . import api


def prepare_publication(manifest_path: str, publishing_user: str):
	api._operator()
	if not publishing_user:
		raise ValueError("Select the publishing account for this manifest")
	return api.begin(json.loads(Path(manifest_path).read_text()), publishing_user)


def _reviewed(handoff: dict) -> tuple[str, dict, str, int, dict]:
	"""The parts of a hand-off: the run, the publication's manifest, the file's fingerprint and size, and what the
	publishing side says of the delegation. A hand-off that is not whole, or not for this site, is refused."""
	import frappe
	from frappe import _

	from .contract import digest
	from .frozen import FORMAT

	manifest = dict(handoff)
	about = manifest.pop("delegation", None) or {}
	run, verified = manifest.pop("run", None), manifest.pop("verified", None)
	sha, size = manifest.pop("file_sha256", None), manifest.pop("file_bytes", None)
	if (
		verified is not True
		or manifest.get("site") != frappe.local.site
		or manifest.get("format") != FORMAT
		or digest(manifest) != run
		or "Organization" not in manifest.get("counts", {})
		or "sensitive_field_policy" not in manifest
	):
		frappe.throw(_("Handoff does not match the reviewed full publication and target site"))
	return run, manifest, sha, size, about


def context_state(context: str, run: str | None = None) -> dict:
	"""Whether a delegation's context can receive a publication: it exists, is locked and hidden, holds no record
	of the publication's tables, and no other run holds it."""
	import frappe
	from frappe import _

	from .frozen import ORDER, selection

	found = frappe.db.get_value(
		"Context",
		context,
		["context_code", "context_name", "context_status", "context_discoverability"],
		as_dict=True,
	)
	if not found:
		return {"exists": False, "problem": None}
	out = {"exists": True, "code": found.context_code, "name": found.context_name, "problem": None}
	other = frappe.db.get_value(
		api.RUN,
		{
			"context_id": context,
			# a proposal holds nothing: several may wait for one delegation, and one is approved
			"state": ["not in", ("Discarded", "Rejected", api.PROPOSED)],
			"name": ["!=", run or ""],
		},
		"display_id",
	)
	if (found.context_status, found.context_discoverability) != ("Locked", "Hidden"):
		out["problem"] = _("The delegation's context is not locked and hidden")
	elif other:
		out["problem"] = _("Run {0} already holds this delegation").format(other)
	else:
		for doctype in ORDER:
			where = selection(doctype)
			# nosemgrep: frappe-semgrep-rules.rules.security.frappe-sql-format-injection -- reviewed: the DocType is one of ORDER and the condition is built by selection() from constants; the context is a parameter
			if frappe.db.sql(
				f"select 1 from `tab{doctype}` r {where} limit 1", (context,) * where.count("%s")
			):
				out["problem"] = _("The delegation's context already holds {0} records").format(_(doctype))
				break
	return out


def inspect_handoff(handoff: dict) -> dict:
	"""What a hand-off says, for the person who is about to approve it: nothing is changed."""
	import frappe

	api._operator()
	run, manifest, sha, size, about = _reviewed(handoff)
	known = frappe.db.get_value(api.RUN, run, ["display_id", "state"], as_dict=True)
	return {
		"run": run,
		"snapshot": manifest["snapshot"],
		"rows": sum(manifest["counts"].values()),
		"batches": manifest["batches"],
		"counts": {table: rows for table, rows in manifest["counts"].items() if rows},
		"bytes": size,
		"sha256": sha,
		"delegation": about,
		"context": {"id": manifest["context"], **context_state(manifest["context"], run)},
		"known": known,
	}


def _prepare_context(context: str, run: str, code: str | None, name: str | None) -> bool:
	"""Make the delegation's context where the site has none: under the published identifier, locked and
	hidden, as a publication needs it. Whether it was made."""
	import frappe
	from frappe import _

	state = context_state(context, run)
	if state["problem"]:
		frappe.throw(state["problem"])
	if state["exists"]:
		return False
	if not (code and name):
		frappe.throw(_("Give the code and the name of the delegation: this site has no context for it yet"))
	record = frappe.get_doc(
		{
			"doctype": "Context",
			"context_code": code,
			"context_name": name,
			"context_status": "Locked",
			"context_discoverability": "Hidden",
		}
	)
	# a Migration Admin prepares the context of the delegation they approve, and no other: the caller has
	# checked the role, and the identifier comes from the reviewed hand-off
	record.insert(set_name=context, ignore_permissions=True)
	return True


def _open_to_approval(run: str) -> str | None:
	"""The state of the run this publication has on the site, where it may still be approved: none yet,
	proposed, or approved before."""
	import frappe
	from frappe import _

	state = frappe.db.get_value(api.RUN, run, "state", for_update=True)
	if state and state not in (api.AWAITING, api.PROPOSED):
		frappe.throw(_("This publication is already on the site, as a run that is {0}").format(_(state)))
	return state


def _await(run: str, manifest: dict, approval: dict) -> str:
	"""Keep the approved publication as a run that waits for its file, so that it shows in the Desk from the
	approval on. Returns the run's short ID."""
	import frappe
	from frappe import _

	from .contract import encoded
	from .ucm_migration_loader import run_overview

	facts = json.dumps(
		{
			"by": frappe.session.user,
			"on": str(frappe.utils.now_datetime()),
			"publisher": approval["publisher"],
			"sha256": approval["sha256"],
			"size": approval["size"],
		}
	)
	state = _open_to_approval(run)
	if state:
		proposal = json.loads(frappe.db.get_value(api.RUN, run, "approval_json") or "{}")
		kept = {key: proposal[key] for key in ("proposed_by", "proposed_on", "delegation") if key in proposal}
		frappe.db.set_value(
			api.RUN,
			run,
			{
				"state": api.AWAITING,
				"approval_json": json.dumps({**kept, **json.loads(facts)}),
				"publishing_user": approval["publisher"],
			},
		)
	else:
		record = frappe.get_doc(
			{
				"doctype": api.RUN,
				"context_id": manifest["context"],
				"snapshot_id": manifest["snapshot"],
				"publishing_user": approval["publisher"],
				"manifest_json": encoded(manifest).decode(),
				"state": api.AWAITING,
				"received": manifest["batches"],
				"applied": 0,
				"document_count": sum(manifest["counts"].values()),
				"approval_json": facts,
			}
		)
		# only the loader's own methods write a run; the Desk reads it
		record.insert(set_name=run, ignore_permissions=True)
	run_overview.refresh(run)
	return frappe.db.get_value(api.RUN, run, "display_id")


def approve(
	handoff: dict, publishing_user: str, context_code: str | None = None, context_name: str | None = None
) -> dict:
	"""Approve a reviewed hand-off for the account that will send its file. The delegation's context is made
	where the site has none, and the publication becomes a run that waits for its file."""
	import frappe
	from frappe import _

	from .publication_transport import approve as bind

	api._operator()
	run, manifest, sha, size, about = _reviewed(handoff)
	if publishing_user == frappe.session.user:
		frappe.throw(_("The account that sends the file cannot be the one that approves it"))
	if publishing_user and api.ROLE in frappe.get_roles(publishing_user):
		frappe.throw(_("A Migration Admin cannot be the publishing account"))
	# before anything is written: an approval's file opens the site to the upload
	_open_to_approval(run)
	made = _prepare_context(
		manifest["context"], run, context_code or about.get("code"), context_name or about.get("name")
	)
	approval = bind(run, publishing_user, sha, size)
	return {
		**approval,
		"display_id": _await(run, manifest, approval),
		"rows": sum(manifest["counts"].values()),
		"batches": manifest["batches"],
		"bytes": size,
		"snapshot": manifest["snapshot"],
		"context_made": made,
	}


def propose(handoff: dict) -> dict:
	"""Keep the hand-off a publishing account proposes as a run that waits for approval. Nothing of the site is
	prepared and no file is accepted for it: that is the approval's."""
	import frappe
	from frappe import _

	from .contract import encoded
	from .ucm_migration_loader import run_overview

	frappe.only_for(api.PUBLISHER)
	run, manifest, sha, size, about = _reviewed(handoff)
	known = frappe.db.get_value(api.RUN, run, ["state", "publishing_user", "display_id"], as_dict=True)
	if known:
		if known.publishing_user != frappe.session.user:
			frappe.throw(_("This publication is another account's"), frappe.PermissionError)
		return {"run": run, "display_id": known.display_id, "state": known.state}
	record = frappe.get_doc(
		{
			"doctype": api.RUN,
			"context_id": manifest["context"],
			"snapshot_id": manifest["snapshot"],
			"publishing_user": frappe.session.user,
			"manifest_json": encoded(manifest).decode(),
			"state": api.PROPOSED,
			"received": manifest["batches"],
			"applied": 0,
			"document_count": sum(manifest["counts"].values()),
			"approval_json": json.dumps(
				{
					"proposed_by": frappe.session.user,
					"proposed_on": str(frappe.utils.now_datetime()),
					"sha256": sha,
					"size": size,
					"delegation": about,
				}
			),
		}
	)
	# only the loader's own methods write a run; the publishing account holds no right on the DocType
	record.insert(set_name=run, ignore_permissions=True)
	run_overview.refresh(run)
	return {"run": run, "display_id": record.display_id, "state": api.PROPOSED}


def _proposed(run: str) -> tuple:
	"""A proposed run, with its manifest and what was proposed."""
	import frappe
	from frappe import _

	record = frappe.get_doc(api.RUN, run)
	if record.state != api.PROPOSED:
		frappe.throw(_("This run is not a proposal waiting for approval"))
	return record, json.loads(record.manifest_json), json.loads(record.approval_json or "{}")


def inspect_proposed(run: str) -> dict:
	"""What a proposed hand-off says, for the Migration Admin who is about to approve it. The file's
	fingerprint is not among it: the approver has its short form from the publishing operator, and types it."""
	api._operator()
	record, manifest, proposal = _proposed(run)
	return {
		"run": run,
		"display_id": record.display_id,
		"snapshot": manifest["snapshot"],
		"rows": sum(manifest["counts"].values()),
		"batches": manifest["batches"],
		"counts": {table: rows for table, rows in manifest["counts"].items() if rows},
		"bytes": proposal.get("size"),
		"delegation": proposal.get("delegation") or {},
		"context": {"id": manifest["context"], **context_state(manifest["context"], run)},
		"proposed_by": proposal.get("proposed_by"),
		"proposed_on": proposal.get("proposed_on"),
	}


def approve_proposed(
	run: str, code: str, context_code: str | None = None, context_name: str | None = None
) -> dict:
	"""Approve a proposed hand-off. The approver proves they heard from the publishing operator themselves:
	they give the check code the operator's command printed, which is the short form of the file's
	fingerprint. A proposal made with the publishing account by someone else carries another code."""
	import frappe
	from frappe import _

	from .contract import check_code

	api._operator()
	record, manifest, proposal = _proposed(run)
	given = "".join(character for character in (code or "").upper() if character.isalnum())
	if given != check_code(proposal["sha256"]).replace("-", ""):
		frappe.throw(
			_(
				"The check code does not match this hand-off. Ask the publishing operator for the code their command gave."
			)
		)
	handoff = {
		"run": run,
		"verified": True,
		"file_sha256": proposal["sha256"],
		"file_bytes": proposal["size"],
		"delegation": proposal.get("delegation"),
		**manifest,
	}
	return approve(handoff, record.publishing_user, context_code, context_name)


def approve_sealed(handoff_path: str, publishing_user: str):
	"""Bench entry point: approve the hand-off in a file on this host."""
	return approve(json.loads(Path(handoff_path).read_text()), publishing_user)


def run_of(handoff_or_run: str) -> str:
	"""The run a site operator names: by the hand-off file they were given, or by the run's identifier."""
	path = Path(handoff_or_run)
	if path.is_file():
		return json.loads(path.read_text())["run"]
	return handoff_or_run


def approve_handoff(handoff_path: str, publishing_user: str) -> dict:
	"""Approve a reviewed hand-off for one publishing account, and say what was approved."""
	return approve_sealed(handoff_path, publishing_user)


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
