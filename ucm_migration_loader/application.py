"""Apply one verified pilot batch per worker transaction, then publish or cancel the run."""

import json
from hashlib import sha256

import frappe

from . import api, reconciliation
from .contract import FIELDS, check_documents, digest

HIDDEN = (
    "Applying",
    "Paused",
    "Ready",
    "Cleaning",
    "Publication staged",
    "Publication applying",
    "Publication cleaning",
    "Publication paused",
    "Publication ready",
)


def lock_run(run):
    context = frappe.db.get_value(api.RUN, run, "context_id")
    ctx = frappe.get_doc("Context", context, for_update=True)
    return ctx, api._record(run)


def guard_context(doc, method=None):
    if doc.is_new() or frappe.flags.ucm_publishing_context == doc.name:
        return
    previous = doc.get_doc_before_save()
    if previous and (
        previous.context_status == "Locked"
        and previous.context_discoverability == "Hidden"
        and (
            doc.context_status != "Locked"
            or doc.context_discoverability != "Hidden"
            or (
                previous.context_description == api.PILOT_MARKER
                and doc.context_description != api.PILOT_MARKER
            )
        )
    ):
        refuse_if_applying(doc.name)


def guard_share(doc, method=None):
    if doc.share_doctype in (*FIELDS, "Person Consent", "Activity") and doc.share_name:
        owner = frappe.db.get_value(
            doc.share_doctype, doc.share_name, "context_tech_id"
        )
        if (
            owner
            and frappe.flags.ucm_publishing_context != owner
            and frappe.db.get_value("Context", owner, "context_status") == "Locked"
        ):
            refuse_if_applying(owner)


def refuse_if_applying(context):
    frappe.db.get_value("Context", context, "name", for_update=True)
    if frappe.db.get_value(
        api.RUN,
        {"context_id": context, "state": ["in", HIDDEN]},
        "name",
        for_update=True,
    ):
        frappe.throw(
            "This migration is applying hidden batches. Activate or discard its run first."
        )


def payload(record, sequence):
    batch = frappe.get_doc(api.BATCH, f"{record.name}-{sequence}", for_update=True)
    manifest = json.loads(record.manifest_json)
    docs = json.loads(batch.payload)
    if (
        digest(manifest) != record.name
        or digest(docs) != manifest["batches"][sequence - 1]
        or digest(docs) != batch.digest
    ):
        raise ValueError("Batch does not match the approved manifest")
    check_documents(docs, record.context_id, manifest["profile"])
    return batch, docs


def fingerprint(context, documents):
    """Hash the stored rows and child rows under lock, including target-derived fields."""
    rows = []
    for doc in documents:
        dt, name = doc["doctype"], doc["name"]
        row = frappe.db.get_value(dt, name, "*", as_dict=True, for_update=True)
        if not row or str(row.context_tech_id) != context:
            raise ValueError("An applied record is missing or changed context")
        rows.append(row)
        if dt == "Person":
            for table in ("Person Other Name", "Person Address"):
                rows.extend(
                    frappe.db.get_values(
                        table,
                        {"parent": name, "parenttype": dt},
                        "*",
                        as_dict=True,
                        order_by="idx, name",
                        for_update=True,
                    )
                )
        if dt == "Case":
            rows.extend(
                frappe.db.get_values(
                    "Context Case",
                    {"parent": name, "parenttype": dt},
                    "*",
                    as_dict=True,
                    order_by="idx, name",
                    for_update=True,
                )
            )
    return sha256(
        json.dumps(rows, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


def check_closed(ctx, *, pilot=True):
    if (
        (pilot and ctx.context_description != api.PILOT_MARKER)
        or ctx.context_status != "Locked"
        or ctx.context_discoverability != "Hidden"
    ):
        raise ValueError("Application requires a locked, hidden migration context")
    if not frappe.db.get_single_value("System Settings", "disable_document_sharing"):
        raise ValueError("Enable the UCM setting that disables Frappe document sharing")
    if frappe.db.exists("Record Share", {"context_tech_id": ctx.name}):
        raise ValueError("A share exists for the incoming context")


def enqueue_batch(run, sequence, stop, dispatch_id):
    frappe.enqueue(
        "ucm_migration_loader.application.apply_batch",
        queue=api.QUEUE,
        run=run,
        sequence=sequence,
        stop=stop,
        dispatch_id=dispatch_id,
        enqueue_after_commit=True,
    )


def start(run, max_batches=None):
    api._operator()
    if max_batches is not None and max_batches < 1:
        raise ValueError("The batch limit must be positive")
    ctx, record = lock_run(run)
    if record.state in ("Activated", "Ready"):
        return api._result(record)
    if record.state not in ("Applying", "Paused", "Verified"):
        raise ValueError("Verify the run before applying it")
    check_closed(ctx)
    if record.state == "Verified":
        api._context(ctx.name)
    dispatch_id = (record.dispatch_id or 0) + 1
    frappe.db.set_value(
        api.RUN, run, {"state": "Applying", "dispatch_id": dispatch_id, "finding": None}
    )
    first = (record.applied or 0) + 1
    stop = (
        min(record.received, first + max_batches - 1)
        if max_batches
        else record.received
    )
    enqueue_batch(run, first, stop, dispatch_id)
    return {
        "run": run,
        "requested": "apply",
        "from_batch": first,
        "through_batch": stop,
    }


def apply_batch(run, sequence, stop, dispatch_id):
    api._operator()
    ctx, record = lock_run(run)
    if (
        record.state != "Applying"
        or dispatch_id != record.dispatch_id
        or sequence != (record.applied or 0) + 1
    ):
        return
    try:
        insert_batch(ctx, record, sequence)
        report = (
            reconciliation.summarize(record) if sequence == record.received else None
        )
    except (
        frappe.ValidationError,
        frappe.DuplicateEntryError,
        frappe.PermissionError,
        ValueError,
    ) as exc:
        frappe.db.rollback()
        _, current = lock_run(run)
        if (
            current.state == "Applying"
            and current.dispatch_id == dispatch_id
            and current.applied == record.applied
        ):
            frappe.db.set_value(
                api.RUN,
                run,
                {
                    "state": "Paused",
                    "finding": f"Apply batch {sequence}: {type(exc).__name__}",
                },
            )
        return
    state = "Ready" if sequence == record.received else "Applying"
    frappe.db.set_value(
        api.RUN,
        run,
        {
            "applied": sequence,
            "state": state,
            "finding": None,
            "reconciliation_json": json.dumps(report) if report else None,
        },
    )
    if sequence < stop:
        enqueue_batch(run, sequence + 1, stop, dispatch_id)


def insert_batch(ctx, record, sequence):
    check_closed(ctx)
    api.check_schema(json.loads(record.manifest_json))
    batch, docs = payload(record, sequence)
    # If the job dies, the rows, application receipt and cursor roll back together.
    # A retry after commit sees the new cursor, even if queuing the next job failed.
    for doc in docs:
        if frappe.db.exists(
            "DocShare", {"share_doctype": doc["doctype"], "share_name": doc["name"]}
        ) or frappe.db.exists(
            "Record Share", {"share_doctype": doc["doctype"], "share_name": doc["name"]}
        ):
            raise ValueError("A published ID already has a share")
        frappe.get_doc(doc).insert(set_name=doc["name"], set_child_names=False)
    target_digest = fingerprint(record.context_id, docs)
    frappe.db.set_value(api.BATCH, batch.name, "target_digest", target_digest)


def check_receipt(record, sequence):
    batch, docs = payload(record, sequence)
    if batch.target_digest != fingerprint(record.context_id, docs):
        raise ValueError(
            f"Stored records differ from application receipt for batch {sequence}"
        )
    return docs


def publish(ctx, record):
    from ucm.controllers.validation import validate_option_sets

    check_closed(ctx)
    api.check_schema(json.loads(record.manifest_json))
    if record.applied != record.received:
        raise ValueError("Application receipts are incomplete")
    current_report = reconciliation.summarize(record)
    if not record.reconciliation_json:
        frappe.db.set_value(
            api.RUN, record.name, "reconciliation_json", json.dumps(current_report)
        )
    elif current_report != json.loads(record.reconciliation_json):
        raise ValueError("Stored data differs from the reconciliation report")
    for sequence in range(1, record.applied + 1):
        _, docs = payload(record, sequence)
        for doc in docs:
            validate_option_sets(frappe.get_doc(doc))
    frappe.flags.ucm_publishing_context = ctx.name
    try:
        ctx.context_status = "Active"
        ctx.save()
    finally:
        frappe.flags.ucm_publishing_context = None
    api._clear_payloads(record.name)
    api.mark_activated(record.name)


def pause(run):
    api._operator()
    _, record = lock_run(run)
    if record.state in ("Paused", "Ready"):
        return api._result(record)
    if record.state != "Applying":
        raise ValueError("Only an applying run can be paused")
    frappe.db.set_value(api.RUN, run, "state", "Paused")
    return {"run": run, "state": "Paused", "applied": record.applied}


def queue_cleanup(run, sequence):
    frappe.enqueue(
        "ucm_migration_loader.application.cleanup_batch",
        queue=api.QUEUE,
        run=run,
        sequence=sequence,
        enqueue_after_commit=True,
    )


def cleanup_batch(run, sequence):
    api._operator()
    ctx, record = lock_run(run)
    if record.state != "Cleaning" or record.applied != sequence:
        return
    try:
        check_closed(ctx)
        for doc in reversed(check_receipt(record, sequence)):
            frappe.delete_doc(
                doc["doctype"],
                doc["name"],
                delete_permanently=True,
                ignore_missing=False,
            )
    except (frappe.ValidationError, frappe.PermissionError, ValueError) as exc:
        frappe.db.rollback()
        _, current = lock_run(run)
        if current.state == "Cleaning" and current.applied == sequence:
            frappe.db.set_value(
                api.RUN,
                run,
                "finding",
                f"Cleanup batch {sequence}: {type(exc).__name__}",
            )
        return
    if sequence == 1:
        api._clear_payloads(run)
    frappe.db.set_value(
        api.RUN,
        run,
        {
            "state": "Cleaning" if sequence > 1 else "Discarded",
            "applied": sequence - 1,
            "finding": None,
        },
    )
    if sequence > 1:
        queue_cleanup(run, sequence - 1)
