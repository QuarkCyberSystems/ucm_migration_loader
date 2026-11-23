"""Evaluate historical share rows against UCM's current record, receiver and consent rules."""

import frappe
from frappe.utils import getdate


class IneligibleShare(ValueError):
    """An eligibility change that keeps a historical share in operator review."""


def check_share(row, owner):
    from ucm.ucm.doctype.record_share.record_share import (
        OPEN,
        SHAREABLE,
        check_user_receiver,
        consent_blocker,
        may_share,
    )
    from ucm.utils import site_today

    dt, name = row["share_doctype"], row["share_name"]
    receiver, user = row["share_to_context_tech_id"], row["share_to_user"]
    if bool(receiver) == bool(user) or receiver == owner:
        raise ValueError(
            "Published share requires one receiver outside the owning context"
        )
    if (
        dt not in SHAREABLE
        or frappe.db.get_value(dt, name, "context_tech_id", for_update=True) != owner
    ):
        raise IneligibleShare("Published share owner differs from stored record")
    if not may_share(owner) or not frappe.has_permission(dt, "read", name):
        raise frappe.PermissionError("Operator cannot share the stored record")
    if row["share_status"] not in OPEN:
        return "historical"
    if (
        receiver
        and frappe.db.get_value("Context", receiver, "context_status", for_update=True)
        != "Active"
    ):
        return "receiver_not_active"
    if row["share_until"] and getdate(row["share_until"]) < getdate(site_today()):
        return "historical"
    if dt == "Person" and frappe.db.get_value(
        dt, name, "person_is_merged", for_update=True
    ):
        raise IneligibleShare("Published open share points to a merged person")
    from ucm.permissions.sharing import SHARE_RECIPIENT

    persons = [name] if dt == "Person" else []
    if dt == "Activity":
        persons = frappe.db.get_values(
            "Activity Detail",
            {"activity_tech_id": name},
            "person_tech_id",
            for_update=True,
            pluck=True,
        )
    for person in sorted({p for p in persons if p}):
        decision = frappe.db.get_value(
            "Person Consent",
            {"person_tech_id": person, "consent_recipient": SHARE_RECIPIENT},
            "consent_decision",
            for_update=True,
        )
        if decision == "Total objection":
            raise IneligibleShare("Current UCM consent prohibits this historical share")
    if consent_blocker(dt, name):
        raise IneligibleShare("Current UCM consent prohibits this historical share")
    if user:
        check_user_receiver(user, owner)
        return "named_user"
    return "active_receiver"
