"""What the rehearsals share: the bench they run on, the site over a loopback TLS listener, accounts made for one
run, a reader of the publishing database, and the calls a Migration Admin makes."""

import json
import math
import os
import re
import secrets
import subprocess
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from threading import Thread
from uuid import uuid4

import frappe
import requests
from werkzeug.serving import make_server

from ucm_migration_loader import api, constants

BENCH = next(root for root in Path(__file__).resolve().parents if (root / "sites" / "apps.txt").is_file())


def call(method, **params):
	try:
		result = getattr(api, method)(**params)
		frappe.db.commit()
		return result
	except Exception:
		frappe.db.rollback()
		raise


def worker():
	frappe.db.commit()
	subprocess.run(["bench", "worker", "--queue", constants.QUEUE, "--burst", "--quiet"], check=True)
	frappe.db.rollback()


@contextmanager
def publication_reader(cur, schema, socket, work):
	reader = "ucm_read_" + uuid4().hex[:16]
	password = secrets.token_urlsafe(32)
	cur.execute("create user %s@'localhost' identified by %s", (reader, password))
	try:
		db_pattern = schema.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%").replace("`", "``")
		cur.execute(f"grant select on `{db_pattern}`.* to %s@'localhost'", reader)
		config = work / "publisher.json"
		config.write_text(
			json.dumps(
				{
					"unix_socket": str(socket),
					"user": reader,
					"password": password,
					"database": schema,
				}
			)
		)
		config.chmod(0o600)
		yield config
	finally:
		cur.execute("drop user %s@'localhost'", reader)


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


@contextmanager
def caseworker_session(caseworker, work):
	with (
		tls_site(frappe.local.site, work) as (url, cert),
		login_caseworker(caseworker, url, cert) as request,
	):
		yield request


@contextmanager
def login_caseworker(caseworker, url, cert):
	from frappe.utils.password import update_password

	password = secrets.token_urlsafe(36)
	update_password(caseworker, password)
	frappe.db.commit()
	with requests.Session() as session:
		session.trust_env = False
		session.verify = str(cert)

		def request(method, path, *, binary=False, **kwargs):
			result = session.request(method, url + path, timeout=15, **kwargs)
			result.raise_for_status()
			return result.content if binary else result.json()

		request("POST", "/api/method/login", json={"usr": caseworker, "pwd": password})
		assert request("GET", "/api/method/frappe.auth.get_logged_user")["message"] == caseworker
		try:
			yield request
		finally:
			request("POST", "/api/method/logout")


def summary(samples):
	grouped = defaultdict(list)
	for phase, operation, duration, ok in samples:
		grouped[phase, operation].append((duration, ok))
	results = {}
	for (phase, operation), values in grouped.items():
		times = sorted(t for t, ok in values if ok)
		metrics = {"requests": len(values), "errors": len(values) - len(times)}
		for percentile in (50, 95, 99):
			metrics[f"p{percentile}_ms"] = (
				round(times[math.ceil(len(times) * percentile / 100) - 1], 1) if times else None
			)
		results.setdefault(phase, {})[operation] = metrics
	return results


def provision(site):
	from frappe.commands.site import new_site

	if not re.fullmatch(r"[a-z0-9-]+\.localhost", site) or (BENCH / "sites" / site).exists():
		raise ValueError("Provisioning requires an unused local rehearsal site name")
	# Invoke the same bench command in process to keep both credentials out of argv.
	previous = Path.cwd()
	try:
		os.chdir(BENCH / "sites")
		options = [
			site,
			"--admin-password",
			secrets.token_urlsafe(40),
			"--install-app",
			"ucm",
			"--install-app",
			"ucm_migration_loader",
		]
		if password := os.environ.get("MARIADB_ROOT_PASS"):
			options.extend(["--db-root-password", password])
		new_site.main(args=options, standalone_mode=False)
	finally:
		os.chdir(previous)
