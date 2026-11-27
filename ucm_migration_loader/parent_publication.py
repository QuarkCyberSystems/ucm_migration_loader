"""Read projected parents with their ordered Frappe child rows in one streaming query."""

from itertools import chain, groupby
from operator import itemgetter

import pymysql

from .contract import OTHER_NAMES, check_uuid

CASE_FIELDS = (
	"name",
	"context_tech_id",
	"case_name",
	"case_number",
	"case_status",
	"case_remark",
)
NAME_FIELDS = (
	"name",
	"context_tech_id",
	"person_file_id",
	"person_name",
	"person_all_names",
)
ALIAS_FIELDS = ("name", "idx", *OTHER_NAMES, "person_legacy_id")


def case_documents(connection, context):
	return _documents(
		connection,
		context,
		"Case",
		CASE_FIELDS,
		"case_contexts",
		"Context Case",
		("name", "idx", "context_tech_id"),
	)


def person_name_documents(connection, context):
	return _documents(
		connection,
		context,
		"Person",
		NAME_FIELDS,
		"person_other_names",
		"Person Other Name",
		ALIAS_FIELDS,
	)


def _documents(connection, context, dt, fields, child_field, child_dt, child_fields):
	check_uuid(context)
	# Every SQL identifier is fixed by one of the two public projections above.
	columns = [
		*(f"p.`{field}`" for field in fields),
		*(f"x.`{field}` as `child_{field}`" for field in child_fields),
	]
	with connection.cursor(pymysql.cursors.SSDictCursor) as cur:
		cur.execute(
			f"""select {", ".join(columns)}, x.parenttype, x.parentfield
            from `tab{dt}` p left join `tab{child_dt}` x on x.parent=p.name
            where p.context_tech_id=%s order by p.name, x.idx, x.name""",
			context,
		)
		for _, group in groupby(cur, key=itemgetter("name")):
			first = next(group)
			children = []
			for row in chain((first,), group):
				if row["child_name"] is None:
					continue
				if (
					row["parenttype"] != dt
					or row["parentfield"] != child_field
					or row["child_idx"] != len(children) + 1
				):
					raise ValueError(f"Invalid published {child_dt} ownership or order")
				children.append({f: row["child_" + f] for f in child_fields})
			yield {
				"doctype": dt,
				**{f: first[f] for f in fields},
				child_field: children,
			}
