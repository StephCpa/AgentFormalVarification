from dataclasses import dataclass
from enum import StrEnum
from .canonical import Reject, amount, digest


class Execution(StrEnum):
    PREPARED = "Prepared"
    DISPATCHED = "Dispatched"
    UNKNOWN = "Unknown"
    CONFIRMED = "Confirmed"
    REJECTED = "RejectedNoEffect"
    PARTIAL = "PartialEffect"
    ABORTED = "AbortedBeforeDispatch"


class Settlement(StrEnum):
    UNSETTLED = "Unsettled"
    PARTIAL = "Partial"
    FINAL = "Final"
    DISPUTED = "Disputed"


@dataclass(frozen=True)
class Request:
    tenant: str
    subject: str
    task: str
    business_key: str
    order: str
    order_version: str
    destination: str
    amount_minor: str
    max_fee_minor: str
    currency: str = "CNY"
    tool: str = "refund"
    adapter_version: str = "sim/0.1"
    schema_version: str = "refund/0.1"

    def payload(self):
        return dict(self.__dict__)

    def validate(self):
        if any(type(v) is not str or not v or len(v) > 256 for v in self.__dict__.values()):
            raise Reject("INVALID_REQUEST")
        if self.currency != "CNY" or self.tool != "refund" or self.adapter_version != "sim/0.1" or self.schema_version != "refund/0.1":
            raise Reject("UNSUPPORTED_GUARANTEE")
        if amount(self.amount_minor) == 0:
            raise Reject("INVALID_AMOUNT")
        amount(self.max_fee_minor)
        digest(self.payload())

    @property
    def request_digest(self):
        self.validate()
        return digest(self.payload())


@dataclass(frozen=True)
class Capability:
    ref: str
    tenant: str
    subject: str
    orders: frozenset[str]
    max_amount: int
    expires: int
    epoch: int


@dataclass(frozen=True)
class DelegationGrant:
    capability_ref: str
    parent_ref: str | None
    account: str
    can_delegate: bool
    tools: frozenset[str]
    effects: frozenset[str]
    revoked: bool = False


@dataclass(frozen=True)
class Evidence:
    ref: str
    request_digest: str
    issuer: str
    predicate: str
    expires: int


@dataclass(frozen=True)
class Approval:
    ref: str
    request_digest: str
    expires: int
    operation_id: str | None = None


@dataclass(frozen=True)
class Account:
    limit: int
    spent: int = 0
    pending: int = 0
    breached: bool = False


@dataclass(frozen=True)
class Reservation:
    account: str
    upper: int
    charged: int = 0
    remaining: int = 0


@dataclass(frozen=True)
class Operation:
    id: str
    permit_ref: str
    request: Request
    capability_ref: str
    evidence_ref: str
    approval_ref: str
    policy: str
    epoch: int
    expires: int
    binding: str
    state: Execution
    settlement: Settlement
    reservations: tuple[Reservation, ...]
    admitted: bool = False
    revision: int = 0
    obligation_due: int | None = None
    ir_digest: str = ""
    contract_digest: str = ""


@dataclass(frozen=True)
class Observation:
    """Trusted adapter observation. Only the trusted plane may submit this."""
    id: str
    operation_id: str
    request_digest: str
    kind: Execution
    cumulative_costs: tuple[tuple[str, int], ...]
    final_cost: bool
    terminal_effect: bool


@dataclass(frozen=True)
class Event:
    sequence: int
    kind: str
    operation_id: str | None
    details: tuple[tuple[str, str], ...]
