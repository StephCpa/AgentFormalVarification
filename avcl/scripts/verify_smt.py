"""Verify pure IR (or --contract) and freeze queries, models and replay evidence."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from avcl.ir import load_predicate
from avcl.canonical import Reject
from avcl.smt_package import write_smt_evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ir", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--contract", action="store_true", help="Check joint contract clauses and conflicts")
    parser.add_argument("--timeout-ms", type=int, default=5000)
    parser.add_argument("--rlimit", type=int, default=1000000)
    parser.add_argument("--max-encoding-steps", type=int, help="Default: 50000 for predicates, 100000 for contracts")
    parser.add_argument("--max-solver-calls", type=int, default=512, help="Contract-wide solver call budget")
    parser.add_argument("--total-timeout-ms", type=int, default=30000, help="Contract-wide time budget")
    args = parser.parse_args()
    # Bound reading before parsing. The IR loader also enforces its own limits.
    with args.ir.open("rb") as source:
        raw = source.read(65537)
    try:
        if len(raw) > 65536: raise Reject("REQUEST_TOO_LARGE")
        if args.contract:
            from avcl.contract import load_contract
            predicate = load_contract(raw.decode("utf-8", errors="strict"))
        else:
            predicate = load_predicate(raw.decode("utf-8", errors="strict"))
    except (Reject, UnicodeError) as exc:
        parser.error("IR rejected: " + (exc.code if isinstance(exc, Reject) else "INVALID_UTF8"))
    steps = args.max_encoding_steps if args.max_encoding_steps is not None else (100000 if args.contract else 50000)
    limits = {"timeout_ms": args.timeout_ms, "rlimit": args.rlimit, "max_encoding_steps": steps}
    if args.contract:
        from avcl.contract_smt_package import write_contract_smt_evidence
        bundle = write_contract_smt_evidence(args.output, predicate, **limits,
                    max_solver_calls=args.max_solver_calls, total_timeout_ms=args.total_timeout_ms)
    else:
        bundle = write_smt_evidence(args.output, predicate, **limits)
    for report in bundle["reports"]:
        print(f"{report['property_id']}: {report['status']} / {report['solver_status']}")
    # 0: all three properties supported; 1: refuted/broken; 2: inconclusive.
    statuses = {r["status"] for r in bundle["reports"]}
    if statuses & {"Violated", "AssumptionBroken"}: return 1
    return 0 if statuses == {"ProvedUnder"} else 2


if __name__ == "__main__":
    sys.exit(main())
