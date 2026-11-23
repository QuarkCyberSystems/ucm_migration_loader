"""Match a published UCM dictionary term on another site without changing either dictionary."""

TERM_FIELDS = (
    "concept_urn",
    "concept_value_urn",
    "concept_value_code",
    "concept_value_version",
    "concept_value_status",
    "concept_value_iscurrent",
    "concept_value_isdeleted",
    "concept_value_usable",
    "concept_value_name",
    "concept_value_type",
)
TERM_COLUMNS = ", ".join(TERM_FIELDS)


def resolve_term(connection, source_id, attributes):
    """Return the verified target ID and match type. Conflicts must never fall through to remapping."""
    with connection.cursor() as cur:
        cur.execute(
            f"select {TERM_COLUMNS} from `tabConcept Value` where name=%s", (source_id,)
        )
        by_id = cur.fetchone()
        if by_id is not None:
            return (
                (source_id, "same_id")
                if tuple(by_id) == tuple(attributes)
                else (None, "conflicting_id")
            )
        concept, urn, _, version, *_ = attributes
        if not concept or not urn or version is None:
            return None, "missing_identity"
        cur.execute(
            f"""select name, {TERM_COLUMNS} from `tabConcept Value`
                where concept_urn=%s and concept_value_urn=%s and concept_value_version=%s limit 2""",
            (concept, urn, version),
        )
        rows = cur.fetchall()
        if not rows:
            return None, "missing"
        if len(rows) != 1:
            return None, "ambiguous"
        target, *stored = rows[0]
        if tuple(stored) != tuple(attributes):
            return None, "conflicting_attributes"
        return target, "matching_versioned_urn"
