"""Independent remote refund state, with server-side idempotency and tombstones.

Does not read or trust the kernel's budget counters or execution result.
Caller may drop the returned observation to simulate loss of a receipt.
"""
from dataclasses import dataclass, replace
from threading import RLock
from .canonical import Reject, amount
from .types import Request, Operation, Observation, Execution, Evidence
from .send_envelope import SendVerifier
from .receipt_envelope import ReceiptSigner


@dataclass(frozen=True)
class Order:
    version: str
    destination: str
    remaining: int


@dataclass(frozen=True)
class RemoteRecord:
    request_digest: str
    kind: Execution
    principal: int
    fee: int
    order: str


class RefundService:
    def __init__(self, orders, fee=0, *, retention_seconds=None, clock=None, verifier=None):
        if type(fee) is not int or fee < 0:
            raise ValueError("invalid fee")
        if retention_seconds is not None and (type(retention_seconds) is not int or retention_seconds<=0 or not callable(clock)):
            raise ValueError("finite retention requires positive duration and a clock")
        if verifier is not None and (type(verifier) is not SendVerifier or retention_seconds is None
                or type(verifier._receipt_signer) is not ReceiptSigner
                or verifier._receipt_signer.tenant!=verifier.tenant or verifier._receipt_signer.issuer!=verifier.receiver):
            raise ValueError("authenticated sending requires a verifier and finite retention")
        self._lock = RLock()
        self._orders = dict(orders)
        self._records = {}
        self._fee = fee
        self._effects = []
        self._retention=retention_seconds
        self._clock=clock
        self._last_clock=0
        self._retained_until={}
        self._bindings={}
        self._verifier=verifier
        self._signed_receipts={}

    @property
    def requires_envelope(self):return self._verifier is not None

    def _purge(self):
        if self._retention is None:return None
        now=self._clock()
        if type(now) is not int or now<0:raise Reject("INVALID_REMOTE_CLOCK")
        if now<self._last_clock:raise Reject("REMOTE_CLOCK_ROLLBACK")
        self._last_clock=now
        for key,until in tuple(self._retained_until.items()):
            if now>=until:
                del self._records[key]
                del self._retained_until[key]
                del self._bindings[key]
                self._signed_receipts.pop(key,None)
        return now

    def _accept_window(self,op,now):
        if self._retention is None:return
        # This is a trusted-host envelope, not a production authenticated packet.
        # Bound deadline is immutable in the host operation/permit record.
        if type(op.expires) is not int or now>=op.expires:raise Reject("REMOTE_ACCEPTANCE_CLOSED")
        if op.id in self._records:
            if self._bindings[op.id]!=op.binding:raise Reject("REMOTE_IDEMPOTENCY_CONFLICT")
        elif now+self._retention<op.expires:
            # Refuse BEFORE any effect: records must outlive packet acceptance.
            raise Reject("REMOTE_RETENTION_TOO_SHORT")

    def _remember_window(self,op,now):
        if self._retention is not None:
            self._retained_until[op.id]=now+self._retention
            self._bindings[op.id]=op.binding

    @property
    def effects(self):
        with self._lock:
            return tuple(self._effects)

    @property
    def real_cost(self):
        with self._lock:
            return sum(p + f for _, _, p, f in self._effects)

    def validate(self, request: Request, evidence_id, expires):
        request.validate()
        with self._lock:
            order = self._orders[request.order]
            if (request.order_version != order.version or request.destination != order.destination or
                    amount(request.amount_minor) > order.remaining or self._fee > amount(request.max_fee_minor)):
                raise Reject("INELIGIBLE")
            return Evidence(evidence_id, request.request_digest, "order-service", "RefundEligible", expires)

    def read_order(self,order_id):
        """Host adapter read; caller must register the immutable snapshot."""
        with self._lock:
            if order_id not in self._orders:raise Reject("UNKNOWN_ORDER")
            return self._orders[order_id],self._fee

    def change_version(self, order_id, version):
        with self._lock:
            self._orders[order_id] = replace(self._orders[order_id], version=version)

    def _receipt(self, operation_id, record):
        observation=Observation("remote:" + operation_id, operation_id, record.request_digest, record.kind,
                           (("tenant:CNY", record.principal + record.fee),
                            ("order:" + record.order + ":CNY", record.principal)), True, True)
        if not self.requires_envelope:return observation
        if operation_id not in self._signed_receipts:
            self._signed_receipts[operation_id]=self._verifier._receipt_signer.seal(observation,self._bindings[operation_id],self._last_clock)
        return self._signed_receipts[operation_id]

    def send(self, op: Operation):
        with self._lock:
            now=self._purge()
            if self.requires_envelope:op=self._verifier.verify(op,now)
            if not op.admitted:
                raise Reject("NOT_ADMITTED")
            self._accept_window(op,now)
            req = op.request
            if op.id in self._records:
                record = self._records[op.id]
                if record.request_digest != req.request_digest:
                    raise Reject("REMOTE_IDEMPOTENCY_CONFLICT")
                return self._receipt(op.id, record)
            order = self._orders[req.order]
            value = amount(req.amount_minor)
            if req.order_version != order.version or req.destination != order.destination or value > order.remaining:
                record = RemoteRecord(req.request_digest, Execution.REJECTED, 0, 0, req.order)
            else:
                record = RemoteRecord(req.request_digest, Execution.CONFIRMED, value, self._fee, req.order)
                self._orders[req.order] = replace(order, remaining=order.remaining - value,
                                                 version=order.version + ":next")
                self._effects.append((op.id, req.order, value, self._fee))
            self._records[op.id] = record
            self._remember_window(op,now)
            return self._receipt(op.id, record)

    def query(self, operation_id):
        with self._lock:
            self._purge()
            if operation_id not in self._records:
                return None  # Absence is not a terminal no-effect proof.
            return self._receipt(operation_id, self._records[operation_id])

    def cancel_with_tombstone(self, op):
        with self._lock:
            if self.requires_envelope:raise Reject("SIGNED_CANCEL_UNSUPPORTED")
            if not op.admitted:
                raise Reject("NOT_ADMITTED")
            now=self._purge()
            self._accept_window(op,now)
            if op.id in self._records and self._records[op.id].request_digest != op.request.request_digest:
                raise Reject("REMOTE_IDEMPOTENCY_CONFLICT")
            if op.id not in self._records:
                self._records[op.id] = RemoteRecord(op.request.request_digest, Execution.REJECTED, 0, 0, op.request.order)
                self._remember_window(op,now)
            return self._receipt(op.id, self._records[op.id])
