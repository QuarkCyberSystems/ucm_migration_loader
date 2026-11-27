"""What the app sets up on a site beside its DocTypes, at install and after every migrate."""

import frappe

# may propose a hand-off and send the file of an approved one, through ucm-push; holds no right in the Desk
PUBLISHER = "Migration Publisher"


def after_install():
	ensure_roles()


def after_migrate():
	ensure_roles()


def ensure_roles() -> None:
	"""The role of a publishing account. The Migration Admin role comes with the DocTypes' permission rows; this
	one reads no DocType, so nothing else brings it."""
	if not frappe.db.exists("Role", PUBLISHER):
		# a role of the app itself, made by the app: whoever installs or migrates may not manage roles
		frappe.get_doc({"doctype": "Role", "role_name": PUBLISHER, "desk_access": 0}).insert(
			ignore_permissions=True
		)
