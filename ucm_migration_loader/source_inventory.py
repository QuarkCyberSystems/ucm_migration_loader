"""Account for delegation business records outside the current casework publication."""

from .contract import check_uuid
from .scope import CATALOGS

# These configure permissions, forms and lookups. The casework transfer must not
# manufacture live user grants from a publishing database.
SETUP_TABLES = frozenset(
	(
		*CATALOGS,
		"Context Profile Exemption",
		"Person Entry Form Context",
		"Political Power",
		"Political Power Precision",
		"Team Security Profile",
		"User Tag Clearance",
	)
)


# a person's file is its picture where the person names it and the file is attached as that field
PICTURE = """p.person_picture is not null and p.person_picture != ''
	and f.attached_to_field = 'person_picture' and f.file_url = p.person_picture"""


# a person's key as a file names it. Where the key is kept as a UUID, as UCM keeps it, comparing it with the
# file's text column as it is would read every file of the database for each query; as text, the files of
# each person are found by their index
PERSON = "cast(p.name as char)"


def person_files(cur, context) -> dict:
	"""What a delegation's persons have attached that a publication cannot carry. It carries a person's
	picture, as one private file, and no other attachment: the site would refuse the person when it is loaded,
	after the whole file was sealed, sent and staged. Each finding has its count and the first person, by
	identifier, for the publishing team to look at."""
	found = {}
	cur.execute(
		f"""select count(*), count(distinct f.attached_to_name), min(f.attached_to_name)
		from tabPerson p straight_join tabFile f
			on f.attached_to_doctype = 'Person' and f.attached_to_name = {PERSON}
		where p.context_tech_id = %s and not ({PICTURE})""",
		(context,),
	)
	files, persons, first = cur.fetchone()
	if files:
		found["other_attachments"] = {"files": files, "persons": persons, "first_person": first}
	cur.execute(
		f"""select count(*), min(name) from (
			select p.name, (
				select count(*) from tabFile f
				where f.attached_to_doctype = 'Person' and f.attached_to_name = {PERSON} and {PICTURE}
					and f.is_private = 1 and f.file_url like '/private/files/%%'
			) files
			from tabPerson p
			where p.context_tech_id = %s and p.person_picture is not null and p.person_picture != ''
		) pictures where files != 1""",
		(context,),
	)
	persons, first = cur.fetchone()
	if persons:
		found["pictures_without_one_private_file"] = {"persons": persons, "first_person": first}
	return found


def files_refusal(found: dict) -> str | None:
	"""The sentence that refuses to seal a delegation for what its persons have attached."""

	def counted(n: int, one: str, many: str) -> str:
		return f"{n:,} {one if n == 1 else many}"

	said = []
	if other := found.get("other_attachments"):
		said.append(
			f"{counted(other['files'], 'file is', 'files are')} attached to "
			f"{counted(other['persons'], 'person', 'persons')} beside the person's picture "
			f"(first person: {other['first_person']})"
		)
	if pictures := found.get("pictures_without_one_private_file"):
		said.append(
			f"the picture of {counted(pictures['persons'], 'person', 'persons')} is not held as one private "
			f"file (first person: {pictures['first_person']})"
		)
	if not said:
		return None
	return (
		"; ".join(said)[:1].upper()
		+ "; ".join(said)[1:]
		+ ". A publication carries a person's picture, as one private file, and no other attachment: "
		"correct this in the publishing database, then seal again"
	)


def inventory(source, context):
	from .frozen import CHILDREN, OWNERS, TABLES

	check_uuid(context)
	with source.cursor() as cur:
		cur.execute("select context_code from tabContext where name=%s", context)
		delegation = cur.fetchone()
		if not delegation:
			raise ValueError("Unknown published delegation")
		cur.execute("""select table_name from information_schema.columns where
            table_schema=database() and column_name='context_tech_id'""")
		direct = {t[3:] for (t,) in cur if t.startswith("tab")}
		counts = {}
		for dt in sorted(direct):
			table = dt.replace("`", "``")
			cur.execute(f"select count(*) from `tab{table}` where context_tech_id=%s", context)
			if count := cur.fetchone()[0]:
				counts[dt] = count

		gaps = {dt: count for dt, count in counts.items() if dt not in TABLES and dt not in SETUP_TABLES}
		sensitive = ("Organization", "Person", "Activity", "Activity Detail")
		cur.execute(
			"""select coalesce(record_doctype,''),count(*) from `tabSensitive Information`
            where context_tech_id=%s and (record_doctype is null or record_doctype not in %s)
            group by record_doctype""",
			(context, sensitive),
		)
		for kind, count in cur:
			gaps[f"Sensitive Information on {kind or '(unspecified)'}"] = count

		for kind in sensitive:
			cur.execute(
				f"""select count(*) from `tabSensitive Information` r
                left join `tab{kind}` p on p.name=r.record_name
                where r.record_doctype=%s and (r.context_tech_id=%s or p.context_tech_id=%s)
                and (p.name is null or not (p.context_tech_id <=> r.context_tech_id))""",
				(kind, context, context),
			)
			if count := cur.fetchone()[0]:
				gaps[f"Sensitive Information with inconsistent {kind} ownership"] = count

		# Link owners select their own delegation. A task or file has no context
		# column, and Context Case.context_tech_id identifies a receiving delegation.
		owners = {
			**OWNERS,
			**{child: (parent, "parent") for parent, children in CHILDREN.items() for child in children},
		}
		for dt, type_field, name_field, carried in (
			("File", "attached_to_doctype", "attached_to_name", ("Person",)),
			("ToDo", "reference_type", "reference_name", ("Person", "Case")),
		):
			cur.execute(f"select distinct `{type_field}` from `tab{dt}` where `{type_field}` is not null")
			for (kind,) in cur.fetchall():
				if kind == "Context":
					cur.execute(
						f"select count(*) from `tab{dt}` where `{type_field}`=%s and `{name_field}`=%s",
						(kind, context),
					)
					if count := cur.fetchone()[0]:
						gaps[f"{dt} on Context"] = count
					continue
				if kind in carried or (kind not in direct and kind not in owners):
					continue
				table = kind.replace("`", "``")
				joins = f"join `tab{table}` p on p.name=r.`{name_field}`"
				owner = "p"
				if kind in owners:
					parent, link = owners[kind]
					joins += f" join `tab{parent}` master on master.name=p.`{link}`"
					owner = "master"
				cur.execute(
					f"select count(*) from `tab{dt}` r {joins} where r.`{type_field}`=%s and {owner}.context_tech_id=%s",
					(kind, context),
				)
				if count := cur.fetchone()[0]:
					gaps[f"{dt} on {kind}"] = count
		return {
			"context": context,
			"context_code": delegation[0],
			"rows_with_context_column": counts,
			"unsupported_business_rows": gaps,
			"unsupported_person_files": person_files(cur, context),
		}
