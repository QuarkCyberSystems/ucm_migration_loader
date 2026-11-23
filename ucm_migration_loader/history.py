"""Insert a published history entry and restore its recorded lock within the insert transaction."""

import frappe
from frappe.utils import get_datetime, now_datetime

from .application import check_closed
from .stored_rows import insert_document


def insert_history(row, *, insert=insert_document):
    from ucm.ucm.doctype.activity_detail.activity_detail import (
        BY,
        LOCKED,
        ON,
        set_locked,
    )

    frappe.only_for("System Manager")
    ctx = frappe.get_doc("Context", row["context_tech_id"], for_update=True)
    check_closed(ctx, pilot=False)
    locked, actor, when = row[LOCKED], row[BY], row[ON]
    if locked not in (0, 1) or (not locked and (actor or when)):
        raise ValueError("Inconsistent published history lock")
    if locked and (
        not actor
        or not when
        or not frappe.db.exists("User", actor)
        or get_datetime(when) > now_datetime()
    ):
        raise ValueError(
            "Published history lock requires a known actor and historical timestamp"
        )
    doc = frappe.get_doc({"doctype": "Activity Detail", **row})
    insert(doc, set_name=row["name"])
    if locked:
        # UCM checks both ordinary write and lock/unlock permission on this new record.
        set_locked(doc.name, 1)
        doc.reload()
        doc.update({BY: actor, ON: when})
        doc.flags.lock_change = True
        doc.save()
    return doc
