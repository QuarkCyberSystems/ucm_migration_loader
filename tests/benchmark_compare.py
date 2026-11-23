"""Compare completed 50k routes after checking they opened the same incoming wave."""

import argparse
import json
from pathlib import Path


def compare(work, raw):
    results = {}
    manifests = {}
    for route in ("orm", "sql"):
        d = json.loads((work / f"{route}-result.json").read_text())
        m = d["manifests"]["CHA"]
        manifests[route] = (m["counts"], m["chain"])
        assert m["counts"]["Person"] == 50000
        assert (
            d["second"]["state"] == "Activated"
            and d["second"]["applied"] == d["second"]["received"]
        )
        assert d["invalid_batch_rejected_before_commit"] == "LinkValidationError"
        assert d["concurrent_caseworkers"] == 4 and d["all_last_saves_verified"]
        assert d["unshared_case_denied_during_and_after_wave"]
        times = d["timings_seconds"]
        phases = [
            p
            for line in (work / f"{route}-phases.jsonl").read_text().splitlines()
            if (p := json.loads(line))["run"] == m["run"]
        ]
        writes = [p for p in phases if p["method"] == "apply"]
        release = next(p for p in phases if p["method"] == "activate")
        results[route] = {
            "site": d["site"],
            "counts": m["counts"],
            "batches": d["second"]["received"],
            "batch_chain": m["chain"],
            "incoming_apply_and_reconcile_seconds": times["second_apply"],
            "incoming_activation_seconds": times["second_activate"],
            "apply_plus_activation_seconds": times["second_apply"]
            + times["second_activate"],
            "business_batch_body_seconds": sum(
                p.get("write_seconds", 0) for p in writes
            ),
            "validation_reconciliation_before_activation_seconds": sum(
                p.get("reconciliation_seconds", 0) for p in writes
            ),
            "activation_reconciliation_seconds": release["reconciliation_seconds"],
            "live_caseworker": d["caseworker"],
            "access_checks": d["access_checks"],
            "invalid_batch_rejected_before_commit": True,
        }
        if route == "sql":
            results[route]["bulk_insert_statement_seconds"] = sum(
                p.get("bulk_sql_seconds", 0) for p in writes
            )
    assert manifests["orm"] == manifests["sql"]
    assert results["orm"]["access_checks"] == results["sql"]["access_checks"]
    raw_result = json.loads(raw.read_text())
    assert {dt: v["rows"] for dt, v in raw_result["tables"].items()} == manifests[
        "orm"
    ][0]
    return {
        "routes": results,
        "raw_copy": raw_result,
        "observed_end_to_end_speedup": results["orm"]["apply_plus_activation_seconds"]
        / results["sql"]["apply_plus_activation_seconds"],
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("work", "raw", "evidence"):
        p.add_argument("--" + name, type=Path, required=True)
    args = p.parse_args()
    result = compare(args.work, args.raw)
    with args.evidence.open("x") as f:
        json.dump(result, f, indent=2)
