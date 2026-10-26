"""Opt-in schema for the current legacy Event payloads, not semantic replay.

No historical bytes are rewritten. Unknown kinds fail closed in this profile;
hash-only verification remains available for older or host-extension events.
Opaque observation repr fields are never executed or treated as structured facts.
"""
import re
from .audit import validate_event
from .canonical import Reject, strict_loads, canonical_bytes
from .requirements import merge_requirements
from .evaluation import WORK_MODEL, MAX_WORK_LIMIT

PROFILE = "avcl/legacy-event-payload/0.2"

# Each alternative is an exact key set. No permissive catch-all properties.
# Scope: required operation, no operation, or either (explicitly partial context).
CATALOG = {
    "CapacityPolicyChanged": ("none", ("operations obligations workflows requirement_id",)),
    "DelegationSubjectRegistered": ("none", ("subject requirement_id",)),
    "DelegationSubjectDisabled": ("none", ("subject requirement_id",)),
    "DelegationRootRegistered": ("none", ("capability_ref account limit requirement_id",)),
    "CapabilityRevoked": ("none", ("capability_ref requirement_id",)),
    "CapabilityDelegated": ("none", ("parent_ref capability_ref subject child_subject account limit request_id requirement_id",)),
    "ApiCredentialIssued": ("none", ("credential subject expires requirement_id",)),
    "ApiCredentialRevoked": ("none", ("credential requirement_id",)),
    "CheckedRequestRegistered": ("none", ("checked_ref subject request_digest requirement_id",)),
    "ReconciliationRequested": ("required", ("request_id reason requirement_id",)),
    "ApiRequestAccepted": ("required", ("subject action request_id trace_id requirement_id",)),
    "ReconcileRequired": ("required", ("due requirement_id",)),
    "SendBlocked": ("required", ("reason requirement_id", "reason at not_after profile requirement_id")),
    "ObservationRejected": ("none", ("digest reason",)),
    "AuthenticatedReceiptRequired": ("required", ("requirement_id",)),
    "ReceiptAuthenticationRejected": ("either", ("reason raw_digest requirement_id",)),
    "ReceiptAuthenticated": ("required", ("envelope_digest observation observed_at requirement_id",)),
    "RejectionAuditConfigured": ("none", ("total_limit subject_limit window_seconds requirement_ids",)),
    "ActionRejected": ("either", ("event_id actor action request_digest trace_id operation_revision policy_digest epoch code requirement_ids time budget_deltas causal_parents evidence_refs",)),
    "Prepared": ("required", ("binding ir_digest requirement_id contract_digest evaluation_work requirement_ids",)),
    "DispatchAdmitted": ("required", ("epoch policy binding ir_digest requirement_id contract_digest evaluation_work requirement_ids",)),
    "AbortedBeforeDispatch": ("required", ("",)),
    "AuthorityRevoked": ("none", ("epoch",)),
    "PolicyActivated": ("none", ("policy contract_digest envelope_digest epoch",)),
    "AssumptionHealthChanged": ("none", ("assumption status expires",)),
    "Unknown": ("required", ("due",)),
    "AssumptionBroken": ("required", ("observation reason",)),
    "ConflictingObservation": ("required", ("observation", "observation fact prior_fact_digest")),
    "ObservationRecorded": ("required", ("observation content", "observation fact effective_costs")),
    "ObservationSubsumed": ("required", ("observation reason",)),
    "Settled": ("required", ("observation",)),
    "PartiallySettled": ("required", ("observation",)),
    "SendAttemptAdmitted": ("required", tuple(
        "at not_after attempt profile envelope_digest send_owner binding" + owner + worker
        for owner in ("", " subject run_id workflow_digest node_id")
        for worker in ("", " worker generation"))),
    "TaskAssessmentChanged": ("none", ("task_ref previous status revision contract_digest requirement_id",)),
    "TaskCloseRequested": ("none", ("task_ref request_id status", "task_ref request_id status trace_id")),
    "TaskRegistered": ("none", ("task_ref subject task_key contract_digest",)),
    "ObligationBreached": ("required", ("due discovered_at requirement_id",)),
    "ObligationResolved": ("required", ("resolution at requirement_id",)),
    "ObligationLeaseExpired": ("required", ("at requirement_id",)),
    "ObligationClaimed": ("required", ("worker generation due requirement_id",)),
    "ReconciliationDeferred": ("required", ("reason requirement_id",)),
    "HandoffReceiverRegistered": ("none", ("receiver expires",)),
    "HandoffAccepted": ("required", ("receiver receipt at requirement_id",)),
    "OrderContextRegistered": ("none", ("context_ref subject order requirement_id",)),
    "OrderContextRevoked": ("none", ("order requirement_id",)),
    "CandidateValidated": ("none", ("checked_ref evidence_ref context_ref request_digest subject request_id requirement_id", "checked_ref evidence_ref context_ref request_digest subject request_id requirement_id trace_id")),
    "WorkflowStarted": ("none", ("subject run_id workflow_digest authenticated_receipts",)),
    "WorkflowStepCommitted": ("either", ("run_id node_id command result",)),
    "WorkflowBackpressured": ("none", ("run_id node_id reason requirement_id",)),
    "WorkflowHalted": ("none", ("run_id node_id reason",)),
    "AdapterObservationRejected": ("required", ("reason requirement_id",)),
    "SendEnvelopeRejected": ("required", ("reason requirement_id",)),
    "AdapterException": ("required", ("error_type",)),
}

DIGESTS = frozenset("binding ir_digest contract_digest request_digest policy_digest envelope_digest workflow_digest digest prior_fact_digest".split())
NUMBERS = frozenset("operations obligations workflows limit expires epoch due at not_after attempt observed_at time revision generation discovered_at total_limit subject_limit window_seconds".split())
TASK_STATUS = frozenset(("Running", "Pending", "Completed", "Rejected", "Escalated", "Failed"))


def fail():
    raise Reject("INVALID_AUDIT_PAYLOAD")


def integer(value, maximum=2**63-1, minimum=0):
    if re.fullmatch(r"0|[1-9][0-9]{0,18}", value) is None: fail()
    number = int(value)
    if not minimum <= number <= maximum: fail()
    return number


def structured(raw):
    try:
        value = strict_loads(raw)
        if canonical_bytes(value).decode("utf-8") != raw: fail()
        return value
    except Reject:
        fail()


def work_report(raw):
    value = structured(raw)
    if type(value) is not dict or set(value) != {"model", "limit", "used", "exhausted"}: fail()
    if value["model"] != WORK_MODEL or value["exhausted"] is not False: fail()
    if type(value["limit"]) is not str or type(value["used"]) is not str: fail()
    limit = integer(value["limit"], MAX_WORK_LIMIT)
    integer(value["used"], limit)


def validate_payload(event):
    """Validate one known current payload. Does not assert event truth or order."""
    validate_event(event)
    if event.kind not in CATALOG: raise Reject("UNSUPPORTED_AUDIT_EVENT_KIND")
    scope, variants = CATALOG[event.kind]
    if scope == "required" and event.operation_id is None: fail()
    if scope == "none" and event.operation_id is not None: fail()
    details = dict(event.details)
    if not any(set(details) == set(variant.split()) for variant in variants): fail()
    for key, value in details.items():
        if not value: fail()
        if key in DIGESTS and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None: fail()
        if key in NUMBERS: integer(value)
        if key == "raw_digest" and value != "unavailable" and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None: fail()
        if key == "requirement_id" and merge_requirements((value,)) != (value,): fail()
        if key == "requirement_ids":
            ids = structured(value)
            if type(ids) is not list or any(type(item) is not str for item in ids) or list(merge_requirements(ids)) != ids: fail()
        if key == "evaluation_work": work_report(value)
        if key == "authenticated_receipts" and value not in ("True", "False"): fail()
        if key == "profile" and value != "refund-send/0.1": fail()
    if event.kind == "ActionRejected":
        if event.operation_id is None:
            if details["operation_revision"] != "null": fail()
        else: integer(details["operation_revision"])
        # This profile describes the present audit-only rejection transaction.
        if details["budget_deltas"] != "{}" or details["causal_parents"] != "[]" or details["evidence_refs"] != "[]": fail()
        if re.fullmatch(r"event-[0-9a-f]{32}", details["event_id"]) is None: fail()
    if event.kind == "RejectionAuditConfigured":
        integer(details["total_limit"], 1000000, 1)
        integer(details["subject_limit"], 1000000, 1)
        integer(details["window_seconds"], 86400, 1)
    if event.kind == "SendAttemptAdmitted":
        owner = "workflow" if "run_id" in details else "direct"
        if details["send_owner"] != owner: fail()
        integer(details["attempt"], 3, 1)
    if event.kind == "CapacityPolicyChanged":
        for key in ("operations", "obligations", "workflows"): integer(details[key], 1000000)
    if event.kind == "AssumptionHealthChanged" and details["status"] not in ("Healthy", "Broken", "Unknown"): fail()
    if event.kind in ("TaskAssessmentChanged", "TaskCloseRequested"):
        if details["status"] not in TASK_STATUS: fail()
        if "previous" in details and details["previous"] not in TASK_STATUS: fail()
    if event.kind == "ObservationSubsumed" and details["reason"] != "same_final_effect_and_cost": fail()
    if event.kind == "ObligationResolved" and details["resolution"] != "FinalEffect": fail()
    if event.kind in ("ObservationRecorded", "ConflictingObservation") and "fact" in details:
        observation_payload(event, details)
    return event


def observation_payload(event, details):
    from .observation_fact import decode_fact, decode_effective_costs, cost_rows
    try:
        fact = decode_fact(details["fact"])
        if fact["observation_id"] != details["observation"] or fact["operation_id"] != event.operation_id: fail()
        if event.kind == "ObservationRecorded":
            if re.fullmatch(r"sha256:[0-9a-f]{64}", fact["request_digest"]) is None: fail()
            if fact["kind"] not in ("Confirmed", "RejectedNoEffect", "PartialEffect"): fail()
            if fact["kind"] in ("Confirmed", "RejectedNoEffect") and not fact["terminal_effect"]: fail()
            cost_rows(fact["cumulative_costs"], unique=True)
            effective = {row["account"]:row["cumulative_cost"] for row in decode_effective_costs(details["effective_costs"])}
            if any(effective.get(row["account"]) != row["cumulative_cost"] for row in fact["cumulative_costs"]): fail()
    except Reject:
        fail()
