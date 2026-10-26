"""Deterministic conservative work accounting for validated pure values.

Units bound interpreter work, not milliseconds, allocation outside evaluation,
or arbitrary host Python. A budget is host-local and shared by all action checks.
"""
from .canonical import Reject
from .requirements import merge_requirements

WORK_MODEL = "avcl/evaluation-work/0.1"
DEFAULT_WORK_LIMIT = 4_000_000
MAX_WORK_LIMIT = 16_000_000


class EvaluationLimit(Reject):
    def __init__(self, limit, used, requested):
        super().__init__("UNKNOWN_EVALUATION")
        self.limit, self.used, self.requested = limit, used, requested
        self.requirement_id = self.node_id = None

    def locate(self, requirement_id, node_id):
        if self.node_id is None:
            self.requirement_id, self.node_id = requirement_id, node_id
            self.requirement_ids = merge_requirements(self.requirement_ids, (requirement_id,))
        return self


class WorkBudget:
    def __init__(self, limit=DEFAULT_WORK_LIMIT):
        if type(limit) is not int or not 0 <= limit <= MAX_WORK_LIMIT:
            raise Reject("INVALID_EVALUATION_BUDGET")
        self.limit, self.used, self.exhausted = limit, 0, False

    def charge(self, units=1):
        if type(units) is not int or units < 0:
            raise Reject("INVALID_EVALUATION_BUDGET")
        if self.exhausted or units > self.limit - self.used:
            self.used, self.exhausted = self.limit, True
            raise EvaluationLimit(self.limit, self.used, units)
        self.used += units

    def report(self):
        return {"model":WORK_MODEL,"limit":str(self.limit),"used":str(self.used),"exhausted":self.exhausted}


def use_budget(budget):
    if budget is None:
        return WorkBudget()
    if type(budget) is not WorkBudget:
        raise Reject("INVALID_EVALUATION_BUDGET")
    return budget


def integer_work(value):
    return 1 + (value.bit_length() + 63) // 64


def value_work(value, budget, depth=0):
    """Comparison/hash upper weight; computing the weight is itself metered.

    Set equality may compare every pair under hash collisions. For equal set
    cardinality n, n * (sum(left weights) + sum(right weights)) bounds those
    comparisons, including nested sets. This deliberately ignores identity and
    hash-cache shortcuts, so the charged result is independent of hash order.
    """
    from .ir_types import Tagged, Some, Record
    budget.charge()
    if depth > 64:
        raise Reject("EVALUATION_TYPE_OR_RANGE")
    if value is None or type(value) is bool:
        return 1
    if type(value) is int:
        return integer_work(value)
    if type(value) is str:
        return 1 + len(value)
    if type(value) is Tagged:
        return 1 + len(value.name) + len(value.value)
    if type(value) is Some:
        return 1 + value_work(value.value, budget, depth + 1)
    if type(value) is Record:
        return 1 + sum(1 + len(k) + value_work(v, budget, depth + 1) for k,v in value.entries)
    if type(value) is frozenset:
        return 1 + (len(value) + 1) * sum(value_work(v, budget, depth + 1) for v in value)
    raise Reject("EVALUATION_TYPE_OR_RANGE")


def record_get(value, name, budget):
    budget.charge((len(value.entries) + 1) * sum(1 + len(k) for k,_ in value.entries))
    return value.get(name)


def binary(op, a, b, budget):
    if op == "EQ":
        budget.charge(value_work(a,budget) + value_work(b,budget))
        return a == b
    if op == "IN":
        # Hash the probe, then allow a collision comparison with every element.
        budget.charge((len(b) + 1) * value_work(a,budget) + value_work(b,budget))
        return a in b
    wa, wb = integer_work(a), integer_work(b)
    budget.charge(wa * wb if op == "MUL" else wa + wb)
    if op == "LE": return a <= b
    if op == "LT": return a < b
    if op == "ADD": return a + b
    if op == "SUB": return a - b
    if op == "MUL": return a * b
    raise Reject("INVALID_CHECK_INSTRUCTION")
