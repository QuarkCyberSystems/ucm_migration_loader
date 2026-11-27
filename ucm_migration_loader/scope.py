"""Check for data a four-table publication would omit before permitting its transfer."""

from .contract import OTHER_NAMES, PROFILE, PROFILES

ORGANIZATION_CATALOGS = ("Political Power", "Political Power Precision", "Global Actor")

CATALOGS = (
	"Context",
	"Protection Department",
	"Field Clearance",
	"General Objective",
	"Specific Objective",
	"Place Type",
	"Activity Precision",
	"Subtarget Population",
	"Service Type",
	"Activity Type",
	"Sensitivity Tag",
	*ORGANIZATION_CATALOGS,
	"Context Profile Exemption",
)

# The fixture generator writes these context-owned rows as well as the four pilot tables.
# Child rows belong to the owning parent, including a Context Case that refers to another context.
RELATED = {
	"Organization": None,
	"Activity": None,
	"Activity Case": ("Case", "case_tech_id"),
	"Activity Detail": ("Activity", "activity_tech_id"),
	"Person Relationship": ("Person", "person_tech_id"),
	"Person Note": ("Person", "person_tech_id"),
	"Activity Note": ("Activity", "activity_tech_id"),
	"Activity Detail Note": ("Activity", "activity_tech_id"),
	"Sensitive Information": None,
	"Record Tag": None,
	"Record Share": None,
	"Person Consent": ("Person", "person_tech_id"),
	"Person Address": ("Person", "parent"),
	"Activity Objective": ("Activity", "parent"),
	"Context Case": ("Case", "parent"),
}

AUDIT = {
	"name",
	"creation",
	"modified",
	"owner",
	"modified_by",
	"idx",
	"docstatus",
	"parent",
	"parentfield",
	"parenttype",
}


PERSON_DEFAULTS = {
	"person_status": "Active",
	"person_birthdate_complete": 1,
	"person_birthdate_unknown": 1,
}


MISSING_PLACE = """from tabActivity a join `tabActivity Type` t on t.name=a.activity_type_tech_id
    where a.context_tech_id=%s and t.activity_type_enabled_place=1
    and t.activity_type_place_required=1 and nullif(cast(a.activity_place_tech_id as char),'') is null"""


def assess(connection, context, profile=PROFILE):
	fields = PROFILES[profile]
	outside, extra = {}, {}
	validation = {}
	incoming_shares = 0
	with connection.cursor() as cur:
		cur.execute(
			"select table_name, column_name, data_type from information_schema.columns where table_schema=database()"
		)
		schema = {}
		for table, field, kind in cur.fetchall():
			schema.setdefault(table, {})[field] = kind

		if {"tabActivity", "tabActivity Type"} <= schema.keys():
			cur.execute(
				"select count(*) " + MISSING_PLACE,
				context,
			)
			if missing := cur.fetchone()[0]:
				validation["activities_missing_required_place"] = missing
		for dt, relation in RELATED.items():
			if dt in fields:
				continue
			table = "tab" + dt
			if table not in schema:
				continue
			clauses, args = [], []
			if "context_tech_id" in schema[table] and dt != "Context Case":
				clauses.append("r.context_tech_id=%s")
				args.append(context)
			if dt == "Record Share":
				cur.execute(
					"select count(*) from `tabRecord Share` where share_to_context_tech_id=%s and not (context_tech_id <=> %s)",
					(context, context),
				)
				incoming_shares = cur.fetchone()[0]
			if relation:
				parent, link = relation
				if "tab" + parent not in schema:
					raise ValueError(f"Cannot establish ownership for {dt} without {parent}")
				clauses.append(
					f"exists (select 1 from `tab{parent}` p where p.name=r.`{link}` and p.context_tech_id=%s)"
				)
				args.append(context)
			cur.execute(f"select count(*) from `{table}` r where " + " or ".join(clauses), args)
			count = cur.fetchone()[0]
			if count:
				outside[dt] = count

		# Frappe stores task assignments and attachment ownership outside the UCM module.
		for dt, type_field, name_field in (
			("File", "attached_to_doctype", "attached_to_name"),
			("ToDo", "reference_type", "reference_name"),
		):
			if "tab" + dt not in schema:
				continue
			count = 0
			for parent in ("Person", "Case", "Activity", "Activity Detail"):
				if "tab" + parent in schema:
					cur.execute(
						f"select count(*) from `tab{dt}` r join `tab{parent}` p on r.`{name_field}`=cast(p.name as char) "
						f"where r.`{type_field}`=%s and p.context_tech_id=%s",
						(parent, context),
					)
					count += cur.fetchone()[0]
			if count:
				outside[dt] = count

		for dt, carried in {**fields, "Person Other Name": OTHER_NAMES}.items():
			table = "tab" + dt
			cols = schema[table]
			omitted = sorted(
				f
				for f in cols
				if f not in (*carried, *AUDIT, "context_tech_id", "person_all_names")
				and not f.startswith("_")
			)
			if not omitted:
				continue
			# In the pilot database defaults for boolean and numeric fields can be zero on all fixtures.
			# Count nonzero numeric fields and nonempty strings. The field inventory also lists zero/default values.
			checks, defaults = [], []
			for f in omitted:
				column = "r.`" + f.replace("`", "``") + "`"
				predicate = (
					f"{column}<>0"
					if cols[f]
					in (
						"int",
						"tinyint",
						"smallint",
						"bigint",
						"decimal",
						"float",
						"double",
					)
					else f"length(cast({column} as char))>0"
				)
				if dt == "Person" and f in PERSON_DEFAULTS:
					predicate = f"{column}<>%s"
					defaults.append(PERSON_DEFAULTS[f])
				checks.append(f"sum({column} is not null and {predicate})")
			source = f"`{table}` r"
			predicate = "r.context_tech_id=%s"
			if dt == "Person Other Name":
				source += " join tabPerson p on p.name=r.parent"
				predicate = "p.context_tech_id=%s"
			elif dt == "Case Member":
				source += " join tabCase c on c.name=r.case_tech_id"
				predicate = "c.context_tech_id=%s"
			cur.execute(
				f"select {','.join(checks)} from {source} where {predicate}",
				(*defaults, context),
			)
			populated = {f: int(n) for f, n in zip(omitted, cur.fetchone(), strict=True) if n}
			if populated:
				extra[dt] = populated
	return {
		"outside_profile_rows": outside,
		"source_validation_findings": validation,
		"incoming_shares_from_other_contexts": incoming_shares,
		"nonempty_unmapped_fields": extra,
		"eligible_for_push": not (validation or outside or incoming_shares or extra),
	}
