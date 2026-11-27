"""Provision the shared country catalog through UCM, preserving published keys and values."""

from decimal import Decimal

import pymysql

COUNTRY_FIELDS = (
	"name",
	"country_iso_2",
	"country_iso_3",
	"country_name",
	"country_name_french",
	"country_name_spanish",
	"country_latitude",
	"country_longitude",
	"country_legacy_id",
)


def read_countries(connection):
	with connection.cursor(pymysql.cursors.DictCursor) as cur:
		cur.execute(f"select {','.join(COUNTRY_FIELDS)} from `tabCountry Territory` order by name")
		return list(cur.fetchall())


def apply_countries(rows):
	"""Caller owns the transaction. Identical rows can be replayed. Divergence aborts it."""
	import frappe

	from .runs import only_admin

	only_admin()
	created = reused = 0

	def read(name):
		return frappe.db.get_value("Country Territory", name, COUNTRY_FIELDS, as_dict=True, for_update=True)

	for row in rows:
		if set(row) != set(COUNTRY_FIELDS):
			raise ValueError("Unexpected country field inventory")
		name = row["name"]

		current = read(name)
		if current is None:
			for key in ("country_iso_2", "country_iso_3"):
				if row[key] and frappe.db.exists("Country Territory", {key: row[key]}):
					raise ValueError(f"Country {key} already belongs to a different target key")
			frappe.get_doc({"doctype": "Country Territory", **row}).insert(set_name=name)
			created += 1
			current = read(name)
		else:
			reused += 1
		if differing := [f for f in COUNTRY_FIELDS if not _same(current[f], row[f])]:
			raise ValueError(f"Country fields differ from the frozen publication: {differing}")
	return {"created": created, "reused": reused}


def _same(target, source):
	if isinstance(source, Decimal) and isinstance(target, float):
		return Decimal(str(target)) == source
	return target == source
