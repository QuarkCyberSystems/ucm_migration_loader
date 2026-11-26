"""Compare published business columns to UCM storage after controller validation."""

from .batch_metrics import measure
from .countries import _same
from .shared_catalogs import SYSTEM


def business(row, child=False):
    omit = SYSTEM if not child else SYSTEM - {"idx"}
    return {k: v for k, v in row.items() if k not in omit and not k.startswith("_")}


def row_differences(dt, row):
    import frappe

    stored = frappe.db.get_value(
        dt, row["name"], list(row), as_dict=True, for_update=True
    )
    if not stored:
        raise ValueError(f"Missing stored {dt}")
    meta = frappe.get_meta(dt)
    differences = {}
    for field, value in row.items():
        if _same(stored[field], value):
            continue
        df = meta.get_field(field)
        if (
            df
            and df.fieldtype == "Select"
            and stored[field] in (None, "")
            and value in (None, "")
        ):
            continue
        differences[field] = (
            "filled_by_target" if value in (None, "") else "changed_published_value"
        )
    return differences


@measure("validation")
def checked_row(dt, row):
    if differences := row_differences(dt, row):
        raise ValueError(
            f"Published {dt} fields differ on target: {sorted(differences)}"
        )


def insert_document(doc, **kwargs):
    return doc.insert(**kwargs)
