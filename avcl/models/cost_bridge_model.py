"""Finite P4 cost bridge: one principal-two, fee-one logical operation.

Remote costs are independent of local spent/pending. No crashes, multiple
operations, receipt reordering, identities, or database refinement are modeled.
"""
from collections import deque
from dataclasses import asdict, dataclass, replace
import json


MODES = ("sound", "early_timeout_release", "final_on_partial",
         "underestimated_upper", "post_final_cost")


@dataclass(frozen=True)
class State:
    phase: str = "Absent"
    principal: int = 0
    fee: int = 0
    spent: int = 0
    pending: int = 0
    closed: bool = False

    @property
    def actual(self):
        return self.principal + self.fee

    @property
    def exposure(self):
        return self.spent + self.pending


def successors(s, mode="sound"):
    if mode not in MODES:
        raise ValueError(mode)
    upper = 2 if mode == "underestimated_upper" else 3
    if s.phase == "Absent":
        yield "Prepare", replace(s, phase="Prepared", pending=upper)
    if s.phase == "Prepared":
        yield "Admit", replace(s, phase="Admitted")
        yield "AbortBeforeAdmission", replace(s, phase="Aborted", pending=0)
    if s.phase == "Admitted":
        yield "LoseReceipt", replace(s, phase="Unknown",
            pending=0 if mode == "early_timeout_release" else s.pending)
    admitted = s.phase in {"Admitted", "Unknown", "Partial", "Final"}
    if admitted and (not s.closed or mode == "post_final_cost"):
        if s.principal == 0:
            yield "RemotePrincipal", replace(s, principal=2)
        if s.fee == 0:
            yield "RemoteFee", replace(s, fee=1)
    if admitted and not s.closed:
        yield "CloseAllRemoteCostSources", replace(s, closed=True)
    if s.phase in {"Admitted", "Unknown", "Partial"}:
        if mode == "final_on_partial":
            yield "MistakePartialForFinal", replace(s, phase="Final", spent=s.actual, pending=0)
        else:
            yield "ObservePartial", replace(s, phase="Partial", spent=s.actual,
                pending=max(0, upper-s.actual))
        if s.closed:
            yield "ObserveFinal", replace(s, phase="Final", spent=s.actual, pending=0)
    if admitted:
        yield "RetryWithoutNewCost", s
    if s.phase == "Final":
        yield "DuplicateFinalReceipt", s


def explore(mode="sound"):
    if mode not in MODES:
        raise ValueError(mode)
    initial = State()
    queue = deque([initial])
    paths = {initial: ()}
    edges = 0
    violations = {}
    while queue:
        s = queue.popleft()
        errors = []
        if s.actual > s.exposure:
            errors.append("actual_exceeds_exposure")
        if s.exposure > 3:
            errors.append("ledger_exceeds_budget")
        for error in errors:
            violations.setdefault(error, {"trace": list(paths[s]), "state": asdict(s),
                                         "actual": s.actual, "exposure": s.exposure})
        for action, nxt in successors(s, mode):
            edges += 1
            if nxt not in paths:
                paths[nxt] = paths[s]+(action,)
                queue.append(nxt)
    report = {"mode": mode, "states": len(paths), "edges": edges,
              "exploration_complete": True, "violations": violations}
    return report, paths


def main():
    reports = [explore(mode)[0] for mode in MODES]
    assert not reports[0]["violations"]
    for report in reports[1:]:
        assert "actual_exceeds_exposure" in report["violations"]
    print(json.dumps({"scope": __doc__, "correct": reports[0],
                      "mutation_oracles": reports[1:], "passed": True}, indent=2))


if __name__ == "__main__":
    main()
