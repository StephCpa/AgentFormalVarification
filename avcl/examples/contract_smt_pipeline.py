"""Freeze joint-policy examples without sealing or activating any policy."""
from dataclasses import replace
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from avcl.contract import Assumption, Clause, build_contract, default_contract
from avcl.contract_smt_package import write_contract_smt_evidence
from avcl.ir import FieldType, compile_predicate


def restriction(name, tag, left, right, events=("prepare", "dispatch")):
    def expr(v): return {"tag": "Field", "name": v} if isinstance(v, str) else {"tag": "Const", "value": v}
    p = compile_predicate({"tag": tag, "args": [expr(left), expr(right)]}, {"amount": FieldType("Int")}, "R15")
    return Clause(name, "safety", "requires", "refund", events, ("R15",), (), False, p)


def examples():
    base = default_contract()
    low = restriction("low", "Le", "amount", 3)
    high = restriction("high", "Lt", 5, "amount")
    def add(clauses, assumptions=None):
        return build_contract(base.module_id, 2, base.tools, base.validators,
                              base.assumptions if assumptions is None else assumptions, base.clauses + tuple(clauses))
    return [
        ("compatible", add([low]), {}, {"prepare": "ProvedUnder", "dispatch": "ProvedUnder", "publication": "ProvedUnder"}),
        ("contradictory", add([low, high]), {}, {"prepare": "Violated", "dispatch": "Violated", "publication": "Violated"}),
        ("phase-only", add([replace(low, events=("prepare",)), replace(high, events=("dispatch",))]), {},
         {"prepare": "ProvedUnder", "dispatch": "ProvedUnder", "publication": "Violated"}),
        ("assumption-conflict", add([], base.assumptions + (Assumption("future-only", "simulator", "refund", 1000, 2000, "oracle"),)), {},
         {"prepare": "ProvedUnder", "dispatch": "ProvedUnder", "publication": "Violated"}),
        ("limited-core", add([low, high]), {"max_solver_calls": 3},
         {"prepare": "Violated", "dispatch": "Unknown", "publication": "Unknown"}),
    ]


def main():
    if len(sys.argv) != 2: raise SystemExit("usage: python examples/contract_smt_pipeline.py OUTPUT_DIRECTORY")
    root = Path(sys.argv[1]); root.mkdir(parents=True, exist_ok=False)
    summary = []
    for name, contract, limits, expected in examples():
        bundle = write_contract_smt_evidence(root / name, contract, **limits)
        availability = {r["selector"]: r for r in bundle["reports"] if r["property_id"].endswith("/joint-allowed-input-exists")}
        statuses = {key: r["status"] for key, r in availability.items()}
        cores = {key: r["conflict"] for key, r in availability.items() if "conflict" in r}
        expected_minimality = "Unconfirmed" if name == "limited-core" else "InclusionMinimal"
        matched = statuses == expected and all(c["minimality"] == expected_minimality for c in cores.values())
        summary.append({"name": name, "statuses": statuses, "expected": expected, "conflicts": cores,
                        "solver_calls": bundle["solver_calls"], "matched": matched})
    (root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if all(item["matched"] for item in summary) else 1


if __name__ == "__main__": sys.exit(main())
