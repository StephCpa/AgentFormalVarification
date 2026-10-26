"""Freeze real-domain, structured-domain, all-deny and limited SMT evidence."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from avcl.ir import FieldType, compile_predicate, refund_predicate
from avcl.smt_package import write_smt_evidence
from examples.ir_pipeline import bounded_refund, c


def main():
    if len(sys.argv) != 2: raise SystemExit("usage: python examples/smt_pipeline.py OUTPUT_DIRECTORY")
    root = Path(sys.argv[1]); root.mkdir(parents=True, exist_ok=False)
    summary = []
    for name, p, limits, expected in [
        ("runtime-refund", refund_predicate(), {}, ["ProvedUnder"] * 3),
        ("bounded-refund", bounded_refund(), {}, ["ProvedUnder"] * 3),
        ("all-deny", compile_predicate(c(False), {"b": FieldType("Bool")}), {},
         ["ProvedUnder", "ProvedUnder", "Violated"]),
        ("encoding-limited", refund_predicate(), {"max_encoding_steps": 1}, ["Unknown"] * 3),
    ]:
        bundle = write_smt_evidence(root / name, p, **limits)
        statuses = [r["status"] for r in bundle["reports"]]
        summary.append({"name": name, "statuses": statuses, "expected": expected, "matched": statuses == expected})
    (root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if all(item["matched"] for item in summary) else 1


if __name__ == "__main__": sys.exit(main())
