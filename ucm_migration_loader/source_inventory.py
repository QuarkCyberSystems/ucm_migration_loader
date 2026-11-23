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
            cur.execute(
                f"select count(*) from `tab{table}` where context_tech_id=%s", context
            )
            if count := cur.fetchone()[0]:
                counts[dt] = count

        gaps = {
            dt: count
            for dt, count in counts.items()
            if dt not in TABLES and dt not in SETUP_TABLES
        }
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
                gaps[f"Sensitive Information with inconsistent {kind} ownership"] = (
                    count
                )

        # Link owners select their own delegation. A task or file has no context
        # column, and Context Case.context_tech_id identifies a receiving delegation.
        owners = {
            **OWNERS,
            **{
                child: (parent, "parent")
                for parent, children in CHILDREN.items()
                for child in children
            },
        }
        for dt, type_field, name_field, carried in (
            ("File", "attached_to_doctype", "attached_to_name", ("Person",)),
            ("ToDo", "reference_type", "reference_name", ("Person", "Case")),
        ):
            cur.execute(
                f"select distinct `{type_field}` from `tab{dt}` where `{type_field}` is not null"
            )
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
        }
