"""Provision a new rehearsal target with the UCM dictionary used by the fixture generator."""

import argparse
import json
import os
from importlib import import_module

import frappe
from generator_snapshot import BENCH
from qualify import provision


def prepare(site):
	if not site.startswith("ucm-"):
		raise ValueError("Choose a new ucm-*.localhost rehearsal site")
	provision(site)
	os.chdir(BENCH / "sites")
	frappe.init(site=site)
	frappe.connect()
	try:
		fixture_demo = import_module(f"{os.environ['UCM_FIXTURE_APP']}.demo")
		from ucm.ucm_dictionary.dictionary import load

		if frappe.db.count("Concept Value") or frappe.db.count("Person"):
			raise ValueError("The reference rehearsal needs an empty target")
		load()
		fixture_demo.ensure_member_roles()
		fixture_demo.ensure_address_types()
		frappe.db.commit()
		return {
			"site": site,
			"dictionary_terms": frappe.db.count("Concept Value"),
			"people": frappe.db.count("Person"),
		}
	finally:
		frappe.destroy()


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--site", required=True)
	print(json.dumps(prepare(parser.parse_args().site)))
