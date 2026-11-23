from uuid import uuid4

import pytest
from ucm_migration_loader.contract import digest, encoded, plan
from ucm_migration_loader.push import send


def test_plan_limits_and_resume():
    context = str(uuid4())
    docs = [
        {
            "doctype": "Person",
            "context_tech_id": context,
            "name": str(uuid4()),
            "person_name": "Synthetic",
            "person_other_names": [],
        }
        for _ in range(3)
    ]
    limit = len(encoded(docs[:1]))
    manifest, batches = plan(
        "ucm.localhost", "snapshot-1", context, docs, byte_limit=limit
    )
    assert list(map(len, batches)) == [1, 1, 1]
    assert (manifest, batches) == plan(
        "ucm.localhost", "snapshot-1", context, docs, byte_limit=limit
    )
    submitted = []

    def call(method, **params):
        if method == "begin":
            return {"run": digest(manifest), "received": 1}
        if method == "upload":
            submitted.append(params["sequence"])
            return {
                "sequence": params["sequence"],
                "digest": digest(params["documents"]),
            }
        return {"state": "Staged"}

    assert send(call, manifest, batches)["state"] == "Staged"
    assert submitted == [2, 3]
    submitted.clear()
    send(call, manifest, batches, max_batches=1)
    assert submitted == [2]
    with pytest.raises(ValueError, match="byte limit"):
        plan("ucm.localhost", "snapshot-1", context, docs, byte_limit=limit - 1)


def test_refuses_missing_membership_and_unexpected_fields():
    context = str(uuid4())
    member = {
        "doctype": "Case Member",
        "name": str(uuid4()),
        "context_tech_id": context,
        "case_tech_id": str(uuid4()),
        "person_tech_id": str(uuid4()),
    }
    with pytest.raises(ValueError, match="prerequisite"):
        plan("ucm.localhost", "snapshot", context, [member])
    with pytest.raises(ValueError, match="Unexpected fields"):
        plan(
            "ucm.localhost",
            "snapshot",
            context,
            [dict(member, ignore_permissions=True)],
        )


def test_new_attempt_keeps_batch_digests_and_published_ids():
    ctx, name = str(uuid4()), str(uuid4())
    docs = [
        {"doctype": "Case", "context_tech_id": ctx, "name": name, "case_name": "Pilot"}
    ]
    first, payloads = plan("ucm.localhost", "same", ctx, docs)
    retry, replay = plan("ucm.localhost", "same", ctx, docs, attempt=2)
    assert digest(first) != digest(retry)
    assert first["batches"] == retry["batches"] and payloads == replay


def test_activity_batches_are_deterministic_and_follow_committed_prerequisites():
    from ucm_migration_loader.activity_order import activity_batches

    opening, request, answer, independent = sorted(str(uuid4()) for _ in range(4))
    rows = [
        (answer, opening, request),
        (request, opening, opening),
        (independent, None, None),
        (opening, None, None),
    ]
    expected = [[opening, independent], [request], [answer]]
    assert (
        list(activity_batches(rows, 2))
        == expected
        == list(activity_batches(reversed(rows), 2))
    )
    with pytest.raises(ValueError, match="missing"):
        list(activity_batches(rows[:-1], 2))
    with pytest.raises(ValueError, match="Cyclic"):
        list(activity_batches([(request, answer, None), (answer, request, None)], 1))


def test_published_consent_requires_an_in_wave_person_and_all_withholding_flags():
    from ucm_migration_loader.contract import CONSENT_PROFILE, WITHHELD

    ctx, person, decision = (str(uuid4()) for _ in range(3))
    parent = {"doctype": "Person", "name": person, "context_tech_id": ctx}
    consent = {
        "doctype": "Person Consent",
        "name": decision,
        "context_tech_id": ctx,
        "person_tech_id": person,
        "consent_decision": "Partial objection",
        "consent_recipient": "Other delegations",
        **dict.fromkeys(WITHHELD, 0),
    }
    with pytest.raises(ValueError, match="Unsupported"):
        plan("target", "snap", ctx, [parent, consent])
    with pytest.raises(ValueError, match="prerequisite"):
        plan("target", "snap", ctx, [consent], profile=CONSENT_PROFILE)
    with pytest.raises(ValueError, match="withholding"):
        plan(
            "target",
            "snap",
            ctx,
            [parent, dict(consent, is_photo_withheld=None)],
            profile=CONSENT_PROFILE,
        )
    manifest, batches = plan(
        "target", "snap", ctx, [parent, consent], profile=CONSENT_PROFILE
    )
    assert manifest["profile"] == CONSENT_PROFILE and batches == [[parent], [consent]]


def test_service_closure_follows_all_members_even_if_its_key_sorts_first():
    from ucm_migration_loader.activity_order import activity_batches

    closing, opening, action = sorted(str(uuid4()) for _ in range(3))
    rows = [
        (closing, opening, None, True),
        (opening, None, None, False),
        (action, opening, None, False),
    ]
    assert list(activity_batches(rows, 25)) == [[opening], [action], [closing]]


def test_schema_ignores_index_declaration_order_only():
    from ucm_migration_loader.contract import table_signatures

    def fingerprint(keys):
        return table_signatures(
            lambda _: (
                "CREATE TABLE `tabT` (\n  `x` int,\n  `y` int,\n"
                + ",\n".join(keys)
                + "\n) ENGINE=InnoDB"
            ),
            ["T"],
            sort_indexes=True,
        )

    first = "  KEY `pair` (`x`,`y`)"
    second = "  UNIQUE KEY `single` (`y`)"
    assert fingerprint([first, second]) == fingerprint([second, first])
    assert fingerprint([first, second]) != fingerprint(
        [first.replace("x`,`y", "y`,`x"), second]
    )
    assert fingerprint([first, second]) != fingerprint([first])
