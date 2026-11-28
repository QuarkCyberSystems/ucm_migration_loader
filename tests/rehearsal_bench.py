"""What the rehearsals share: the site over a loopback TLS listener, and a publishing account made for one run."""

import subprocess
from contextlib import contextmanager
from threading import Thread
from uuid import uuid4

import frappe
from werkzeug.serving import make_server


@contextmanager
def tls_site(site, work, *, threaded=False):
	"""Expose the actual Frappe WSGI app on a loopback TLS listener for this test only."""
	import frappe.app

	cert, key = work / "localhost.pem", work / "localhost.key"
	subprocess.run(
		[
			"openssl",
			"req",
			"-x509",
			"-newkey",
			"rsa:2048",
			"-nodes",
			"-days",
			"1",
			"-subj",
			"/CN=localhost",
			"-addext",
			"subjectAltName=DNS:localhost",
			"-keyout",
			str(key),
			"-out",
			str(cert),
		],
		check=True,
		capture_output=True,
	)
	old_site = frappe.app._site
	frappe.app._site = site
	try:
		with make_server(
			"127.0.0.1",
			0,
			frappe.app.application,
			threaded=threaded,
			ssl_context=(str(cert), str(key)),
		) as server:
			thread = Thread(target=server.serve_forever)
			thread.start()
			try:
				yield f"https://localhost:{server.server_port}", cert
			finally:
				server.shutdown()
				thread.join()
	finally:
		frappe.app._site = old_site


@contextmanager
def publisher_identity():
	from frappe.core.doctype.user.user import generate_keys
	from frappe.utils.password import remove_encrypted_password

	email = f"ucm-push-{uuid4().hex}@example.invalid"
	account = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": "Synthetic Publisher",
			"user_type": "Website User",
			"send_welcome_email": 0,
			"restrict_ip": "127.0.0.1",
			"sso_api_key_allowed": 1,
		}
	)
	account.insert()
	try:
		keys = generate_keys(email)
		frappe.db.commit()
		yield email, keys["api_key"] + ":" + keys["api_secret"]
	finally:
		frappe.db.rollback()
		if frappe.db.exists("User", email):
			account = frappe.get_doc("User", email)
			account.enabled = 0
			account.api_key = ""
			account.api_secret = ""
			account.save()
			remove_encrypted_password("User", email, "api_secret")
			frappe.db.commit()
