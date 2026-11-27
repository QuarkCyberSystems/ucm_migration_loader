"""Build a new frozen synthetic publishing site on the separate local MariaDB instance."""

import argparse
import getpass
import json
import os
import re
import secrets
from importlib import import_module
from pathlib import Path

import frappe
import pymysql
from frappe.commands.site import new_site
from frappe.installer import update_site_config
from generator_snapshot import BENCH

PRESETS = {
	"wave50k": {
		"persons": 50500,
		"contexts": 3,
		"consents": 35,
		"label": "migration-wave-50k",
	},
	"scale": {
		"persons": 2400,
		"contexts": 3,
		"consents": 35,
		"label": "migration-scale-2400",
	},
	"casework": {"persons": 96, "contexts": 2, "label": "migration-source-96"},
	# The third delegation selected by the generator uses local-script person names.
	"access": {
		"persons": 240,
		"contexts": 3,
		"consents": 35,
		"label": "migration-access-240",
	},
}


def generate(site, socket, preset):
	volume = import_module(f"{os.environ['UCM_FIXTURE_APP']}.demo.volume")

	if not re.fullmatch(r"ucm-generator-[a-z0-9-]+\.localhost", site) or Path(site).exists():
		raise ValueError("Choose an unused ucm-generator-*.localhost site")
	with (
		pymysql.connect(unix_socket=socket, user=getpass.getuser()) as admin,
		admin.cursor() as cur,
	):
		cur.execute("select @@skip_networking")
		if cur.fetchone() != (1,):
			raise ValueError("Select the socket-only publishing MariaDB instance")
	new_site.main(
		args=[
			site,
			"--db-host",
			"localhost",
			"--db-socket",
			socket,
			"--db-root-username",
			getpass.getuser(),
			# The local MariaDB administrator authenticates by Unix identity. A nonempty
			# argument keeps bench from prompting for a password during unattended setup.
			"--db-root-password",
			secrets.token_urlsafe(32),
			"--admin-password",
			secrets.token_urlsafe(40),
			"--install-app",
			"ucm",
			"--install-app",
			os.environ["UCM_FIXTURE_APP"],
		],
		standalone_mode=False,
	)
	frappe.init(site=site, force=True)
	try:
		for key, value in (
			("maintenance_mode", 1),
			("developer_mode", 1),
			("ucm_demo_password", secrets.token_urlsafe(40)),
		):
			update_site_config(key, value)
		frappe.init(site=site, force=True)
		frappe.connect()
		if (
			Path(frappe.conf.db_socket).resolve() != Path(socket)
			or not frappe.db.sql("select @@skip_networking")[0][0]
		):
			raise ValueError("Site must be on the dedicated publishing instance")
		values = {
			**PRESETS[preset],
			"users": 12,
			"analysts": 2,
			"user_shares": 2,
			"parallel": 1,
			"keep_indexes": True,
		}
		if preset == "wave50k":
			from unittest.mock import patch

			# Keep the existing catalog distribution and generator rules. Concentrate
			# the people in the incoming CHA wave, with a smaller live BOL neighbour.
			_, weights, _ = volume._persons_per_context(50500, 3, 100)
			with patch.object(
				volume,
				"_persons_per_context",
				return_value=([250, 250, 50000], weights, 50500),
			):
				volume.load(**values)
		else:
			volume.load(**values)
		result = volume.check()
		if not result["ok"]:
			raise ValueError("Generated publication failed integrity checks")
		return {
			"site": site,
			"settings": values,
			"integrity_checks": len(result["zero"]),
		}
	finally:
		frappe.destroy()


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--site", required=True)
	parser.add_argument("--publisher-socket", required=True, type=Path)
	parser.add_argument("--preset", required=True, choices=PRESETS)
	args = parser.parse_args()
	socket = str(args.publisher_socket.resolve(strict=True))
	os.chdir(BENCH / "sites")
	print(json.dumps(generate(args.site, socket, args.preset)))
