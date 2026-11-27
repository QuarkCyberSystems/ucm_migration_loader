"""Compare an independently reviewed set of expected pilot rows with the target's retained report."""

from .contract import CONSENT_PROFILE, OTHER_NAMES, PROFILES, check_uuid, digest


def entity_summary(rows):
	rows = sorted(rows, key=lambda row: row["name"])
	return {
		"rows": len(rows),
		"keys_sha256": digest([row["name"] for row in rows]),
		"matched_fields_sha256": digest(rows),
	}


def compare_expected(expected, report):
	reconciled = report.get("reconciliation")
	if report["state"] not in ("Ready", "Activated") or not reconciled:
		raise ValueError("A reconciled Ready or Activated run is required")
	for key in ("site", "context", "snapshot"):
		if expected.get(key) != reconciled[key]:
			raise ValueError(f"Expected publication targets a different {key}")
	fields = PROFILES[CONSENT_PROFILE]
	types = set(reconciled["entities"])
	if types not in [{*projection, "Person Other Name"} for projection in PROFILES.values()]:
		raise ValueError("Target returned an incomplete or unknown entity inventory")
	tables = expected.get("entities")
	if not isinstance(tables, dict) or set(tables) != types:
		raise ValueError("Supply the expected rows for every pilot table")
	differences = {}
	for dt in types:
		rows = tables[dt]
		if not isinstance(rows, list):
			raise TypeError(f"Expected rows must be a list for {dt}")
		allowed = (
			{"name", "context_tech_id", *fields[dt]}
			if dt in fields
			else {"name", "parent", "idx", *OTHER_NAMES}
		)
		normalized = []
		seen = set()
		for row in rows:
			if not isinstance(row, dict) or set(row) - allowed:
				raise ValueError(f"Unexpected expected fields for {dt}")
			check_uuid(row.get("name"))
			if row["name"] in seen:
				raise ValueError(f"Duplicate expected ID for {dt}")
			seen.add(row["name"])
			if dt in fields and row.get("context_tech_id") != expected["context"]:
				raise ValueError(f"Expected row belongs to another context for {dt}")
			if dt == "Person Other Name":
				check_uuid(row.get("parent"))
				if type(row.get("idx")) is not int or row["idx"] < 1:
					raise ValueError("Supply an alias's positive integer position")
			normalized.append({k: str(v) for k, v in row.items() if v not in (None, "")})
		summary = entity_summary(normalized)
		diff = [key for key in summary if summary[key] != reconciled["entities"][dt][key]]
		if diff:
			differences[dt] = diff
	return {
		"run": reconciled["run"],
		"site": reconciled["site"],
		"context": reconciled["context"],
		"snapshot": reconciled["snapshot"],
		"matched": not differences,
		"differences": differences,
	}
