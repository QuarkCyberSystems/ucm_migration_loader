"""Target-side pilot. Batch receipts acknowledge staging. Activation commits a small wave atomically."""

import json

import frappe

from .contract import (
	MAX_DOCUMENTS,
	MAX_RUN_BYTES,
	PROFILE,
	check_documents,
	check_manifest,
	digest,
	encoded,
	schema_signatures,
)

RUN = "UCM Migration Run"
BATCH = "UCM Migration Batch"
QUEUE = "ucm_migration"
PILOT_MARKER = "Synthetic UCM migration rehearsal"


def _operator():
	frappe.only_for("System Manager")


def check_schema(manifest):
	if "schema" in manifest:
		deployed = schema_signatures(lambda sql: frappe.db.sql(sql)[0][1], manifest["profile"])
		for dt, expected in manifest["schema"].items():
			if deployed[dt] != expected:
				raise ValueError(f"Publishing and target DDL differ for {dt}")


def _publisher_or_operator(run):
	"""A publisher can upload to and inspect only the exact run an operator assigned."""
	user = frappe.session.user
	if user != "Guest" and frappe.db.get_value(RUN, run, "publishing_user") == user:
		return
	_operator()


def _context(context):
	ctx = frappe.get_doc("Context", context, for_update=True)
	if (
		ctx.context_description != PILOT_MARKER
		or ctx.context_status != "Locked"
		or ctx.context_discoverability != "Hidden"
	):
		raise ValueError("Select a locked, hidden context explicitly provisioned for the synthetic pilot")
	for dt in ("Person", "Case", "Case Member", "Person Consent"):
		if frappe.db.exists(dt, {"context_tech_id": context}):
			raise ValueError("The pilot context already contains case-file records")
	return ctx


def _record(run):
	return frappe.get_doc(RUN, run, for_update=True)


def _result(record):
	return {
		"run": record.name,
		"state": record.state,
		"received": record.received,
		"applied": record.applied or 0,
		"documents": record.document_count,
		"finding": record.finding,
		"reconciled_batches": record.get("reconcile_cursor") or 0,
		"worker_phase": record.get("worker_phase"),
		"last_checkpoint": str(record.worker_seen) if record.get("worker_seen") else None,
	}


@frappe.whitelist(methods=["POST"])
def begin(manifest: dict, publishing_user: str | None = None):
	check_manifest(manifest)
	if manifest["site"] != frappe.local.site:
		raise ValueError("Manifest targets a different site")
	run = digest(manifest)
	if not publishing_user and frappe.db.exists(RUN, run):
		_publisher_or_operator(run)
		return _result(_record(run))
	_operator()
	check_schema(manifest)
	if publishing_user and (
		publishing_user == "Guest" or not frappe.db.get_value("User", publishing_user, "enabled")
	):
		raise ValueError("Select an enabled publishing user")
	# Serialize starts, including duplicate submissions, on the pre-provisioned context.
	frappe.db.get_value("Context", manifest["context"], "name", for_update=True)
	if frappe.db.get_value(RUN, run, "name", for_update=True):
		record = _record(run)
		if publishing_user and record.publishing_user != publishing_user:
			raise ValueError("Run already prepared for a different publisher")
		return _result(record)
	_context(manifest["context"])
	if frappe.db.get_value(
		RUN,
		{"context_id": manifest["context"], "state": ["!=", "Discarded"]},
		"name",
		for_update=True,
	):
		raise ValueError("A run already exists for this context")
	record = frappe.get_doc(
		{
			"doctype": RUN,
			"context_id": manifest["context"],
			"snapshot_id": manifest["snapshot"],
			"publishing_user": publishing_user,
			"manifest_json": encoded(manifest).decode(),
			"state": "Receiving",
			"received": 0,
			"document_count": 0,
		}
	)
	# Operators read the audit DocTypes in Desk. Only these controlled methods write them.
	record.insert(set_name=run, ignore_permissions=True)
	return _result(record)


@frappe.whitelist(methods=["POST"])
def status(run: str):
	_publisher_or_operator(run)
	return _result(frappe.get_doc(RUN, run))


@frappe.whitelist(methods=["POST"])
def report(run: str):
	_publisher_or_operator(run)
	record = frappe.get_doc(RUN, run)
	from .frozen import FORMAT
	from .held_shares import summary

	holds = {"held_shares": summary(run)} if json.loads(record.manifest_json).get("format") == FORMAT else {}
	return {
		**_result(record),
		**holds,
		"reconciliation": json.loads(record.reconciliation_json) if record.reconciliation_json else None,
		"activated_by": record.approved_by,
		"activated_on": record.activated_on,
	}


@frappe.whitelist(methods=["POST"])
def quality(run: str):
	"""The run's data quality metrics, for the migration side to keep beside its own."""
	from .ucm_migration_loader import run_overview

	_publisher_or_operator(run)
	record = frappe.get_doc(RUN, run)
	if not run_overview.is_publication(json.loads(record.manifest_json)):
		raise ValueError("Metrics are kept for sealed publications")
	if not record.reconciliation_json:
		raise ValueError("The run has no verification report yet")
	document = run_overview.quality(record)
	return {**document, "sha256": digest(document["metrics"])}


@frappe.whitelist(methods=["POST"])
def quality_received(run: str, sha256: str, rows: int, destination: str):
	"""Record that the migration side stored the metrics it was given, so the run shows the hand-over."""
	from .ucm_migration_loader import run_overview

	current = quality(run)
	if sha256 != current["sha256"] or int(rows) != len(current["metrics"]):
		raise ValueError("The run has changed since these metrics were read")
	handover = {
		"rows": int(rows),
		"sha256": sha256,
		"destination": str(destination)[:140],
		"by": frappe.session.user,
		"on": str(frappe.utils.now_datetime()),
	}
	# the run itself has not changed: its metrics keep the time of its last step
	frappe.db.set_value(RUN, run, "quality_json", json.dumps(handover), update_modified=False)
	run_overview.refresh(run)
	return handover


def mark_activated(run):
	frappe.db.set_value(
		RUN,
		run,
		{
			"state": "Activated",
			"approved_by": frappe.session.user,
			"activated_on": frappe.utils.now_datetime(),
			"finding": None,
		},
	)


@frappe.whitelist(methods=["POST"])
def upload(run: str, sequence: int, documents: list):
	_publisher_or_operator(run)
	record = _record(run)
	manifest = json.loads(record.manifest_json)
	check_documents(documents, record.context_id, manifest["profile"])
	hashes = manifest["batches"]
	checksum = digest(documents)
	if not 1 <= sequence <= len(hashes) or checksum != hashes[sequence - 1]:
		raise ValueError("Batch does not match the frozen plan")
	if record.state == "Discarded":
		raise ValueError("Run discarded")
	key = f"{run}-{sequence}"
	if sequence <= record.received:
		if frappe.db.get_value(BATCH, key, "digest", for_update=True) != checksum:
			raise ValueError("Receipt mismatch")
		return {"sequence": sequence, "digest": checksum}
	if record.state != "Receiving" or sequence != record.received + 1:
		raise ValueError("Expected the next batch of a receiving run")
	count = record.document_count + len(documents)
	payload = encoded(documents)
	used = frappe.db.sql(
		"select coalesce(sum(octet_length(payload)),0) from `tabUCM Migration Batch` where migration_run=%s for update",
		run,
	)[0][0]
	if count > MAX_DOCUMENTS or used + len(payload) > MAX_RUN_BYTES:
		raise ValueError("Run exceeds the pilot limits")
	frappe.get_doc(
		{
			"doctype": BATCH,
			"migration_run": run,
			"sequence_number": sequence,
			"digest": checksum,
			"payload": payload.decode(),
		}
	).insert(set_name=key, ignore_permissions=True)
	frappe.db.set_value(
		RUN,
		run,
		{
			"received": sequence,
			"document_count": count,
			"state": "Staged" if sequence == len(hashes) else "Receiving",
		},
	)
	return {"sequence": sequence, "digest": checksum}


def _queue(run, *, activate):
	_operator()
	record = _record(run)
	if activate and record.state == "Verified" and json.loads(record.manifest_json)["profile"] != PROFILE:
		raise ValueError("Apply and reconcile every batch before activating this profile")
	required = "Verified" if activate else "Staged"
	if record.state != required and not (activate and record.state == "Ready"):
		if record.state in (("Activated",) if activate else ("Verified", "Activated")):
			return _result(record)
		raise ValueError(f"Run must be {required}")
	frappe.enqueue(
		"ucm_migration_loader.api.process",
		queue=QUEUE,
		run=run,
		activate=activate,
		enqueue_after_commit=True,
	)
	return {"run": run, "requested": "activate" if activate else "verify"}


@frappe.whitelist(methods=["POST"])
def verify(run: str):
	return _queue(run, activate=False)


def _full_request(run, action, max_batches=None):
	from .publication_jobs import is_publication, request

	_operator()
	if is_publication(run):
		return request(run, action, max_batches)


@frappe.whitelist(methods=["POST"])
def apply(run: str, max_batches: int | None = None):
	from .application import start

	if full := _full_request(run, "apply", max_batches):
		return full
	return start(run, max_batches)


@frappe.whitelist(methods=["POST"])
def pause(run: str):
	from .application import pause as pause_application

	if full := _full_request(run, "pause"):
		return full
	return pause_application(run)


@frappe.whitelist(methods=["POST"])
def activate(run: str):
	if full := _full_request(run, "activate"):
		return full
	return _queue(run, activate=True)


@frappe.whitelist(methods=["POST"])
def discard(run: str):
	from .application import HIDDEN, lock_run, queue_cleanup

	if full := _full_request(run, "discard"):
		return full
	_operator()
	_, record = lock_run(run)
	if record.state == "Activated":
		raise ValueError("An activated pilot cannot be discarded")
	if record.state in HIDDEN and record.applied:
		frappe.db.set_value(RUN, run, "state", "Cleaning")
		queue_cleanup(run, record.applied)
		return {"run": run, "state": "Cleaning", "applied": record.applied}
	_clear_payloads(run)
	from .publication_receiver import remove_after_discard

	remove_after_discard(record)
	frappe.db.set_value(RUN, run, "state", "Discarded")
	return {"run": run, "state": "Discarded"}


def _clear_payloads(run):
	frappe.db.sql("update `tabUCM Migration Batch` set payload=null where migration_run=%s", run)


def process(run, activate=False):
	"""A queued job owns its transaction. A verify job rolls back all UCM document writes."""
	from ucm.controllers.validation import validate_option_sets

	_operator()
	required = "Verified" if activate else "Staged"
	context = frappe.db.get_value(RUN, run, "context_id")
	frappe.db.get_value("Context", context, "name", for_update=True)
	record = _record(run)
	if activate and record.state == "Ready":
		from .application import publish

		try:
			publish(frappe.get_doc("Context", context, for_update=True), record)
		except (frappe.ValidationError, ValueError) as exc:
			frappe.db.rollback()
			if _record(run).state == "Ready":
				frappe.db.set_value(
					RUN,
					run,
					"finding",
					f"Activation reconciliation: {type(exc).__name__}",
				)

		return
	if record.state != required:
		return
	where = "context preflight"
	try:
		ctx = _context(context)
		manifest = json.loads(record.manifest_json)
		if digest(manifest) != run:
			raise ValueError("Manifest mismatch")
		check_schema(manifest)
		documents = []
		for i, expected in enumerate(manifest["batches"], 1):
			where = f"batch {i}"
			payload = json.loads(frappe.db.get_value(BATCH, f"{run}-{i}", "payload"))
			if digest(payload) != expected:
				raise ValueError("Receipt mismatch")
			check_documents(payload, context, manifest["profile"])
			for doc in payload:
				where = f"dictionary preflight, batch {i}, {doc['doctype']} {doc['name']}"
				document = frappe.get_doc(doc)
				validate_option_sets(document)
				documents.append((i, document))
		seen = set()
		for i, document in documents:
			dt, name = document.doctype, document.name
			where = f"batch {i}, {dt} {name}"
			if dt == "Person Consent" and ("Person", document.person_tech_id) not in seen:
				raise ValueError("Consent references a person outside this run")
			if dt == "Case Member" and (
				("Case", document.case_tech_id) not in seen or ("Person", document.person_tech_id) not in seen
			):
				raise ValueError("Membership references a record outside this run")
			document.insert(set_name=name, set_child_names=False)
			seen.add((dt, name))
		if len(seen) != record.document_count:
			raise ValueError("Document count mismatch")
		if activate:
			ctx.context_status = "Active"
			ctx.save()
			_clear_payloads(run)
			mark_activated(run)
			return
	except (
		frappe.ValidationError,
		frappe.DuplicateEntryError,
		frappe.LinkValidationError,
		frappe.PermissionError,
		ValueError,
	) as exc:
		frappe.db.rollback()
		if _record(run).state == required:
			frappe.db.set_value(
				RUN,
				run,
				{"state": "Rejected", "finding": f"{where}: {type(exc).__name__}"},
			)
		return
	frappe.db.rollback()
	if _record(run).state == required:
		frappe.db.set_value(RUN, run, "state", "Verified")
