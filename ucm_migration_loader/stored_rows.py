"""Compare published business columns to UCM storage after controller validation."""

from decimal import Decimal

from .batch_metrics import measure

# the columns Frappe keeps on every row, which are not business values
SYSTEM = {"owner", "modified_by", "creation", "modified", "idx", "docstatus"}


def same(target, source):
	"""Whether a stored value is the published one: MariaDB gives a decimal where Frappe stores a float."""
	if isinstance(source, Decimal) and isinstance(target, float):
		return Decimal(str(target)) == source
	return target == source


def business(row, child=False):
	omit = SYSTEM if not child else SYSTEM - {"idx"}
	return {k: v for k, v in row.items() if k not in omit and not k.startswith("_")}


def row_differences(dt, row):
	import frappe

	stored = frappe.db.get_value(dt, row["name"], list(row), as_dict=True, for_update=True)
	if not stored:
		raise ValueError(f"Missing stored {dt}")
	meta = frappe.get_meta(dt)
	differences = {}
	for field, value in row.items():
		if same(stored[field], value):
			continue
		df = meta.get_field(field)
		if df and df.fieldtype == "Select" and stored[field] in (None, "") and value in (None, ""):
			continue
		differences[field] = "filled_by_target" if value in (None, "") else "changed_published_value"
	return differences


@measure("validation")
def checked_row(dt, row):
	if differences := row_differences(dt, row):
		raise ValueError(f"Published {dt} fields differ on target: {sorted(differences)}")


def insert_document(doc, **kwargs):
	return doc.insert(**kwargs)
