"""Restricted canonical encoding: ASCII object keys, no JSON numbers.

This is a deliberately restricted binding profile, not a general JCS library.
All quantities at the request boundary are canonical decimal strings.
"""
import hashlib
import json
import re
from .requirements import merge_requirements, requirements_for


class Reject(ValueError):
    def __init__(self, code: str, *, requirement_ids=()):
        super().__init__(code)
        self.code = code
        self.requirement_ids = merge_requirements(requirements_for(code), requirement_ids)


def amount(value: str) -> int:
    if type(value) is not str or not re.fullmatch(r"0|[1-9][0-9]{0,17}", value):
        raise Reject("INVALID_AMOUNT")
    return int(value)


def cumulative_cost(value: str) -> int:
    """Bounded aggregate domain; two legal 18-digit components always fit.

    This does not widen request components or timestamps. Values above the
    operation's admitted bound are decoded so the ledger can record an incident.
    """
    if type(value) is not str or not re.fullmatch(r"0|[1-9][0-9]{0,18}", value):
        raise Reject("INVALID_CUMULATIVE_COST")
    return int(value)


def _validate(value):
    if value is None or type(value) is bool:
        return
    if type(value) is str:
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeError:
            raise Reject("INVALID_UNICODE") from None
    elif type(value) is list:
        for item in value:
            _validate(item)
    elif type(value) is dict:
        for key, item in value.items():
            if type(key) is not str or not key.isascii():
                raise Reject("INVALID_KEY")
            _validate(item)
    else:
        raise Reject("UNSUPPORTED_CANONICAL_VALUE")


def canonical_bytes(value) -> bytes:
    try:
        _validate(value)
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except RecursionError:
        raise Reject("REQUEST_TOO_DEEP") from None
    if len(encoded) > 65536:
        raise Reject("REQUEST_TOO_LARGE")
    return encoded


def digest(value, domain="avcl/request/0.1") -> str:
    return "sha256:" + hashlib.sha256(domain.encode("ascii") + b"\x00" + canonical_bytes(value)).hexdigest()


def strict_loads(raw: str):
    if type(raw) is not str:
        raise Reject("INVALID_JSON")
    try:
        if len(raw.encode("utf-8")) > 65536:
            raise Reject("REQUEST_TOO_LARGE")
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise Reject("DUPLICATE_KEY")
                result[key] = value
            return result
        def bad_number(_):
            raise Reject("JSON_NUMBERS_UNSUPPORTED")
        value = json.loads(raw, object_pairs_hook=pairs, parse_int=bad_number,
                           parse_float=bad_number, parse_constant=bad_number)
        canonical_bytes(value)
        return value
    except (json.JSONDecodeError, UnicodeError, RecursionError):
        raise Reject("INVALID_JSON") from None
