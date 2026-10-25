"""Host-only HMAC policy authority and checked release envelopes.

The in-memory backend uses ephemeral keys; the PostgreSQL adapter persists its
local key in the authority row. HMAC authenticates one local authority domain,
not a public-key publisher or a real human review. Production key isolation
and review identity remain deployment work.
"""
from dataclasses import dataclass
import hashlib
import hmac
import secrets
from .canonical import Reject,canonical_bytes,digest,strict_loads
from .contract import (Contract,Tool,Validator,compile_contract,load_contract,assumptions_valid,
                       moment,names,decimal,exact)
from .checks import COMPILER,load_checks

FORMAT="avcl/signed-contract/0.3"

@dataclass(frozen=True)
class Release:
    compiled: object
    issued_at: int
    expires: int
    envelope_digest: str
    envelope: str

class PolicyAuthority:
    def __init__(self,tools,validators,key=None,key_id="local-authority"):
        if key is None: key=secrets.token_bytes(32)
        if type(key) is not bytes or len(key)<32 or not names((key_id,)): raise Reject("INVALID_SIGNING_CONFIGURATION")
        for items,cls in [(tools,Tool),(validators,Validator)]:
            if type(items) is not tuple or any(type(v) is not cls for v in items) or not names(tuple(v.id for v in items)):
                raise Reject("INVALID_AUTHORITY_REGISTRY")
        self._key=key;self.key_id=key_id
        self._tools={t.id:t for t in tools};self._validators={v.id:v for v in validators}

    def _dependencies(self,contract,health,now):
        for tool in contract.tools:
            registered=self._tools.get(tool.id)
            if registered is None or tool.version!=registered.version or set(tool.effects)!=set(registered.effects):
                raise Reject("TOOL_DEPENDENCY_MISMATCH")
        for validator in contract.validators:
            if self._validators.get(validator.id)!=validator: raise Reject("VALIDATOR_DEPENDENCY_MISMATCH")
        if not assumptions_valid(contract,tuple(a.id for a in contract.assumptions),health,now):
            raise Reject("ASSUMPTION_BROKEN")

    def seal(self,contract,health,now,expires,reviewed_requirements,runtime_clauses,witnesses):
        if not moment(now) or not moment(expires) or not now<expires: raise Reject("INVALID_POLICY_VALIDITY")
        compiled=compile_contract(contract);contract=compiled.contract
        self._dependencies(contract,health,now)
        if type(reviewed_requirements) is not frozenset or reviewed_requirements!=contract.requirements():
            raise Reject("INCOMPLETE_REQUIREMENT_REVIEW")
        required=frozenset(c.id for c in contract.clauses if c.kind=="safety")
        if type(runtime_clauses) is not frozenset or runtime_clauses!=required: raise Reject("HARD_CLAUSE_WITHOUT_APPROVED_CHECK")
        if type(witnesses) is not dict or set(witnesses)!={t.id for t in contract.tools}: raise Reject("MISSING_NONVACUITY_WITNESS")
        encoded={}
        for tool in contract.tools:
            schema={}
            for event in ("prepare","dispatch"):
                for name,t in contract.input_types(tool.id,event).items():
                    if name in schema and schema[name]!=t: raise Reject("CONFLICTING_CONTEXT_TYPE")
                    schema[name]=t
                if not compiled.evaluate(event,tool.id,witnesses[tool.id],health,now)["allowed"]:
                    raise Reject("INVALID_NONVACUITY_WITNESS")
            if type(witnesses[tool.id]) is not dict or set(witnesses[tool.id])!=set(schema): raise Reject("INVALID_NONVACUITY_WITNESS")
            encoded[tool.id]={k:t.value_wire(t.normalize(witnesses[tool.id][k])) for k,t in sorted(schema.items())}
        approval={"key_id":self.key_id,"issued_at":str(now),"expires":str(expires),
                  "reviewed_requirements":sorted(reviewed_requirements),"runtime_clauses":sorted(runtime_clauses),
                  "witnesses":encoded}
        body={"format":FORMAT,"compiler":COMPILER,"contract":strict_loads(contract.to_json()),
              "checks":{name:strict_loads(check.to_json()) for name,check in compiled.checks},"approval":approval}
        signature=hmac.new(self._key,b"avcl/policy-signature/0.3\x00"+canonical_bytes(body),hashlib.sha256).hexdigest()
        return canonical_bytes({**body,"signature":signature}).decode("utf-8")

    def load(self,raw,health,now):
        obj=exact(strict_loads(raw),"format compiler contract checks approval signature")
        if (obj["format"]!=FORMAT or obj["compiler"]!=COMPILER or type(obj["signature"]) is not str or
                len(obj["signature"])!=64 or any(c not in "0123456789abcdef" for c in obj["signature"])):
            raise Reject("UNSUPPORTED_SIGNED_POLICY")
        body={k:v for k,v in obj.items() if k!="signature"}
        expected=hmac.new(self._key,b"avcl/policy-signature/0.3\x00"+canonical_bytes(body),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(obj["signature"],expected): raise Reject("INVALID_POLICY_SIGNATURE")
        a=exact(obj["approval"],"key_id issued_at expires reviewed_requirements runtime_clauses witnesses")
        issued_at,expires=decimal(a["issued_at"]),decimal(a["expires"])
        if a["key_id"]!=self.key_id or not moment(now) or not issued_at<=now<expires: raise Reject("POLICY_EXPIRED_OR_NOT_YET_VALID")
        contract=load_contract(canonical_bytes(obj["contract"]).decode("utf-8"));compiled=compile_contract(contract)
        self._dependencies(contract,health,now)
        if type(obj["checks"]) is not dict or set(obj["checks"])!={c.id for c in contract.clauses}: raise Reject("MISSING_CLAUSE_ARTIFACT")
        for c in contract.clauses: load_checks(canonical_bytes(obj["checks"][c.id]).decode("utf-8"),c.predicate)
        if a["reviewed_requirements"]!=sorted(contract.requirements()) or a["runtime_clauses"]!=sorted(c.id for c in contract.clauses if c.kind=="safety"):
            raise Reject("POLICY_APPROVAL_MISMATCH")
        if type(a["witnesses"]) is not dict or set(a["witnesses"])!={t.id for t in contract.tools}: raise Reject("MISSING_NONVACUITY_WITNESS")
        for tool in contract.tools:
            schema={**contract.input_types(tool.id,"prepare"),**contract.input_types(tool.id,"dispatch")}
            encoded=a["witnesses"][tool.id]
            if type(encoded) is not dict or set(encoded)!=set(schema): raise Reject("INVALID_NONVACUITY_WITNESS")
            values={k:t.value_from_wire(encoded[k]) for k,t in schema.items()}
            for event in ("prepare","dispatch"):
                if not compiled.evaluate(event,tool.id,values,health,now)["allowed"]: raise Reject("INVALID_NONVACUITY_WITNESS")
        return Release(compiled,issued_at,expires,digest(obj,"avcl/policy-envelope/0.3"),canonical_bytes(obj).decode("utf-8"))

    def restore(self,raw):
        """Authenticate a historical snapshot at its signed issuance time.

        This reconstructs what the authority approved, not present health.
        Live admission must use separately persisted current Health records.
        """
        obj=strict_loads(raw)
        contract=load_contract(canonical_bytes(obj["contract"]).decode("utf-8"))
        from .contract import Health
        historical={a.id:Health(a.authority,a.scope,a.valid_from,a.expires,"Healthy") for a in contract.assumptions}
        return self.load(raw,historical,decimal(obj["approval"]["issued_at"]))
