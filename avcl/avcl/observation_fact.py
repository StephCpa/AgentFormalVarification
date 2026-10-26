"""Canonical, data-only observation evidence for new audit events.

The fact records what was submitted, including malformed binding/cost semantics
in same-id conflicts. Validation is deliberately not an authenticity claim.
Costs are decimal strings, with the existing canonical document size bound;
the request/receipt amount limits must not hide an above-bound incident here.
"""
import re
from .canonical import Reject, canonical_bytes, strict_loads, digest
from .types import Observation, Execution

FORMAT = "avcl/observation-fact/0.1"
DOMAIN = "avcl/observation-fact-digest/0.1"
FIELDS = frozenset(("format", "observation_id", "operation_id", "request_digest",
                    "kind", "cumulative_costs", "final_cost", "terminal_effect"))


def text(value):
    return type(value) is str and 0 < len(value) <= 256 and not any(0xD800 <= ord(c) <= 0xDFFF for c in value)


def cost_rows(value, *, unique=False, ordered=False):
    if type(value) is not list: raise Reject("INVALID_OBSERVATION_FACT")
    accounts = []
    for row in value:
        if type(row) is not dict or set(row) != {"account", "cumulative_cost"}: raise Reject("INVALID_OBSERVATION_FACT")
        cost = row["cumulative_cost"]
        if (not text(row["account"]) or type(cost) is not str or
                re.fullmatch(r"0|[1-9][0-9]*", cost) is None): raise Reject("INVALID_OBSERVATION_FACT")
        accounts.append(row["account"])
    if unique and len(accounts) != len(set(accounts)): raise Reject("INVALID_OBSERVATION_FACT")
    if ordered and accounts != sorted(accounts): raise Reject("INVALID_OBSERVATION_FACT")
    return value


def validate_fact(value):
    if type(value) is not dict or set(value) != FIELDS or value["format"] != FORMAT:
        raise Reject("INVALID_OBSERVATION_FACT")
    if (not all(text(value[key]) for key in ("observation_id", "operation_id", "request_digest")) or
            type(value["kind"]) is not str or value["kind"] not in {str(k) for k in Execution} or
            type(value["final_cost"]) is not bool or type(value["terminal_effect"]) is not bool):
        raise Reject("INVALID_OBSERVATION_FACT")
    cost_rows(value["cumulative_costs"])
    return value


def decode_fact(raw):
    try:
        value = validate_fact(strict_loads(raw))
        if canonical_bytes(value).decode("utf-8") != raw: raise Reject("INVALID_OBSERVATION_FACT")
        return value
    except Reject:
        raise Reject("INVALID_OBSERVATION_FACT") from None


def encode_fact(observation):
    if (type(observation) is not Observation or type(observation.kind) is not Execution or
            type(observation.cumulative_costs) is not tuple or any(
                type(pair) is not tuple or len(pair) != 2 or type(pair[1]) is not int or pair[1] < 0
                for pair in observation.cumulative_costs)):
        raise Reject("INVALID_OBSERVATION_FACT")
    value = {"format":FORMAT, "observation_id":observation.id, "operation_id":observation.operation_id,
             "request_digest":observation.request_digest, "kind":str(observation.kind),
             "cumulative_costs":[{"account":account, "cumulative_cost":str(cost)} for account,cost in observation.cumulative_costs],
             "final_cost":observation.final_cost, "terminal_effect":observation.terminal_effect}
    return canonical_bytes(validate_fact(value)).decode("utf-8")


def fact_digest(raw):
    return digest(decode_fact(raw), DOMAIN)


def encode_effective_costs(costs):
    if type(costs) is not dict or any(not text(a) or type(c) is not int or c < 0 for a,c in costs.items()):
        raise Reject("INVALID_OBSERVATION_FACT")
    rows = [{"account":account, "cumulative_cost":str(cost)} for account,cost in sorted(costs.items())]
    return canonical_bytes(rows).decode("utf-8")


def decode_effective_costs(raw):
    try:
        rows = cost_rows(strict_loads(raw), unique=True, ordered=True)
        if canonical_bytes(rows).decode("utf-8") != raw: raise Reject("INVALID_OBSERVATION_FACT")
        return rows
    except Reject:
        raise Reject("INVALID_OBSERVATION_FACT") from None
