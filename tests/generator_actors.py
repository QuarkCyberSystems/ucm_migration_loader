"""Supply inactive synthetic assignment recipients for rollback-only UCM tests."""

import frappe


def ensure_actor(source, name):
    if not name or frappe.db.exists("User", name):
        return
    with source.cursor() as cur:
        cur.execute(
            "select email,first_name,last_name from tabUser where name=%s", name
        )
        row = cur.fetchone()
    if not row or row[0] != name:
        raise ValueError("Published actor has no matching account")
    frappe.get_doc(
        {
            "doctype": "User",
            "email": name,
            "first_name": row[1],
            "last_name": row[2],
            "enabled": 0,
            "user_type": "Website User",
            "send_welcome_email": 0,
        }
    ).insert()
    if frappe.db.get_value("User", name, "enabled"):
        raise ValueError("Historical rehearsal actor must remain disabled")
