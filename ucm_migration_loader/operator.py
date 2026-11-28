"""The hand-off of a sealed publication on the site: proposed by its publishing account, approved by a Migration
Admin, and staged when its file has arrived."""

import json
from pathlib import Path

import frappe
from frappe import _

from . import constants, runs
from .contract import check_code, digest, encoded
from .frozen import FORMAT
from .publication_receiver import context_state
from .publication_transport import approve as bind
from .publication_transport import folder, progress, queue_staging, receive
from .ucm_migration_loader import run_overview


def _reviewed(handoff: dict) -> tuple[str, dict, str, int, dict]:
	"""The parts of a hand-off: the run, the publication's manifest, the file's fingerprint and size, and what the
	publishing side says of the delegation. A hand-off that is not whole, or not for this site, is refused."""
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


def _summary(run: str, manifest: dict, size, about: dict) -> dict:
	"""What a hand-off says of its publication, and whether the site can receive it. The same for a hand-off read
	from its file and for a proposed one."""
	return {
		"run": run,
		"snapshot": manifest["snapshot"],
		"rows": sum(manifest["counts"].values()),
		"batches": manifest["batches"],
		"bytes": size,
		"delegation": about or {},
		"context": {"id": manifest["context"], **context_state(manifest["context"], run)},
	}


def inspect_handoff(handoff: dict) -> dict:
	"""What a hand-off says, for the person who is about to approve it: nothing is changed."""
	runs.only_admin()
	run, manifest, sha, size, about = _reviewed(handoff)
	known = frappe.db.get_value(constants.RUN, run, ["display_id", "state"], as_dict=True)
	return {**_summary(run, manifest, size, about), "sha256": sha, "known": known}


def _prepare_context(context: str, run: str, code: str | None, name: str | None) -> bool:
	"""Make the delegation's context where the site has none: under the published identifier, locked and
	hidden, as a publication needs it. Whether it was made."""
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
	state = frappe.db.get_value(constants.RUN, run, "state", for_update=True)
	if state and state not in (constants.AWAITING, constants.PROPOSED):
		frappe.throw(_("This publication is already on the site, as a run that is {0}").format(_(state)))
	return state


def _await(run: str, manifest: dict, approval: dict) -> str:
	"""Keep the approved publication as a run that waits for its file, so that it shows in the Desk from the
	approval on. Returns the run's short ID."""
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
		proposal = json.loads(frappe.db.get_value(constants.RUN, run, "approval_json") or "{}")
		kept = {key: proposal[key] for key in ("proposed_by", "proposed_on", "delegation") if key in proposal}
		frappe.db.set_value(
			constants.RUN,
			run,
			{
				"state": constants.AWAITING,
				"approval_json": json.dumps({**kept, **json.loads(facts)}),
				"publishing_user": approval["publisher"],
			},
		)
	else:
		record = frappe.get_doc(
			{
				"doctype": constants.RUN,
				"context_id": manifest["context"],
				"snapshot_id": manifest["snapshot"],
				"publishing_user": approval["publisher"],
				"manifest_json": encoded(manifest).decode(),
				"state": constants.AWAITING,
				"received": manifest["batches"],
				"applied": 0,
				"document_count": sum(manifest["counts"].values()),
				"approval_json": facts,
			}
		)
		# only the loader's own methods write a run; the Desk reads it
		record.insert(set_name=run, ignore_permissions=True)
	run_overview.refresh(run)
	return frappe.db.get_value(constants.RUN, run, "display_id")


def approve(
	handoff: dict, publishing_user: str, context_code: str | None = None, context_name: str | None = None
) -> dict:
	"""Approve a reviewed hand-off for the account that will send its file. The delegation's context is made
	where the site has none, and the publication becomes a run that waits for its file."""
	runs.only_admin()
	run, manifest, sha, size, about = _reviewed(handoff)
	if publishing_user == frappe.session.user:
		frappe.throw(_("The account that sends the file cannot be the one that approves it"))
	if publishing_user and constants.ROLE in frappe.get_roles(publishing_user):
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
	frappe.only_for(constants.PUBLISHER)
	run, manifest, sha, size, about = _reviewed(handoff)
	known = frappe.db.get_value(constants.RUN, run, ["state", "publishing_user", "display_id"], as_dict=True)
	if known:
		if known.publishing_user != frappe.session.user:
			frappe.throw(_("This publication is another account's"), frappe.PermissionError)
		return {"run": run, "display_id": known.display_id, "state": known.state}
	record = frappe.get_doc(
		{
			"doctype": constants.RUN,
			"context_id": manifest["context"],
			"snapshot_id": manifest["snapshot"],
			"publishing_user": frappe.session.user,
			"manifest_json": encoded(manifest).decode(),
			"state": constants.PROPOSED,
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
	return {"run": run, "display_id": record.display_id, "state": constants.PROPOSED}


def _proposed(run: str) -> tuple:
	"""A proposed run, with its manifest and what was proposed."""
	record = frappe.get_doc(constants.RUN, run)
	if record.state != constants.PROPOSED:
		frappe.throw(_("This run is not a proposal waiting for approval"))
	return record, json.loads(record.manifest_json), json.loads(record.approval_json or "{}")


def inspect_proposed(run: str) -> dict:
	"""What a proposed hand-off says, for the Migration Admin who is about to approve it. The file's
	fingerprint is not among it: the approver has its short form from the publishing operator, and types it."""
	runs.only_admin()
	record, manifest, proposal = _proposed(run)
	return {
		**_summary(run, manifest, proposal.get("size"), proposal.get("delegation")),
		"display_id": record.display_id,
		"proposed_by": proposal.get("proposed_by"),
	}


def approve_proposed(
	run: str, code: str, context_code: str | None = None, context_name: str | None = None
) -> dict:
	"""Approve a proposed hand-off. The approver proves they heard from the publishing operator themselves:
	they give the check code the operator's command printed, which is the short form of the file's
	fingerprint. A proposal made with the publishing account by someone else carries another code."""
	runs.only_admin()
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


def request_staging(run: str) -> dict:
	"""Asked from the Desk: have the migration worker stage the arrived file of a run that waits for it, or
	that the site refused before. The worker does it, as a file of many gigabytes takes longer to check than
	a request may last; the run shows the outcome. A run that is staged already says so at once."""
	runs.only_admin()
	approval = folder(run) / "approval.json"
	if not approval.is_file():
		return {"outcome": "not approved", "run": run}
	approved = json.loads(approval.read_bytes())
	arrived = progress(folder(run), approved)
	if arrived["offset"] != arrived["size"]:
		return {"outcome": "incomplete", "run": run, "arrived": arrived["offset"], "bytes": arrived["size"]}
	if frappe.db.get_value(constants.RUN, run, "state") not in (constants.AWAITING, constants.REJECTED):
		return stage_arrived(run)
	queue_staging(run, approved["size"], frappe.session.user)
	return {"outcome": "queued", "run": run}


def stage_arrived(handoff_or_run: str) -> dict:
	"""Stage the file of an approved run once it has arrived, and say how it went: staged, refused by the
	site's checks (the refused run is kept), not arrived completely, or never approved here."""
	runs.only_admin()
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
		kept = frappe.db.get_value(constants.RUN, run, ["display_id", "state", "finding"], as_dict=True)
		if not kept or kept.state != constants.REJECTED or "preflight failed" not in str(refused):
			raise
		return {"outcome": "refused", "run": run, "display_id": kept.display_id, "finding": kept.finding}
	record = frappe.db.get_value(
		constants.RUN, run, ["display_id", "document_count", "received"], as_dict=True
	)
	return {
		"outcome": "staged",
		"run": run,
		"display_id": record.display_id,
		"state": result["state"],
		"rows": record.document_count,
		"batches": record.received,
	}
