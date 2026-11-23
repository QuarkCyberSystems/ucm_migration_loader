"""Read published consent decisions and withholding flags with their owning person's delegation."""

import pymysql

from .contract import CONSENT_FIELDS, WITHHELD, check_uuid

__all__ = ["CONSENT_FIELDS", "WITHHELD", "consent_documents"]


def consent_documents(connection, context):
    check_uuid(context)
    with connection.cursor(pymysql.cursors.SSDictCursor) as cur:
        fields = ", ".join(f"r.`{f}`" for f in CONSENT_FIELDS)
        cur.execute(
            f"""select {fields}, p.context_tech_id as person_context from `tabPerson Consent` r
                left join tabPerson p on p.name=r.person_tech_id
                where p.context_tech_id=%s or r.context_tech_id=%s order by r.name""",
            (context, context),
        )
        for row in cur:
            if row.pop("person_context") != context or row["context_tech_id"] not in (
                None,
                context,
            ):
                raise ValueError(
                    "Consent and person must belong to the same published delegation"
                )
            row["context_tech_id"] = context
            if row["consent_date"] is not None:
                row["consent_date"] = row["consent_date"].isoformat()
            yield {"doctype": "Person Consent", **row}
