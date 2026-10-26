"""Offline local VCs for predicate/0.2. Never called by an execution gate.

AST and actual generated instructions are translated separately. Shared type,
value and primitive encodings, Python and Z3 remain explicit trusted code.
No independent proof-certificate checker is claimed.
"""
from datetime import datetime, timezone
from hashlib import sha256
from importlib.metadata import version, PackageNotFoundError
import platform

from .canonical import Reject, canonical_bytes, digest
from .checks import compile_checks, COMPILER
from .ir import BOOL, SEMANTICS
from .ir_types import FieldType, Record, Some, Tagged
from .evaluation import WorkBudget, EvaluationLimit, DEFAULT_WORK_LIMIT, WORK_MODEL

BUILD = "avcl/smt/0.1"
Z3_PACKAGE = "4.15.4.0"
Z3_CORE = "4.15.4"
PROPERTIES = ("input-domain-inhabited", "checks-equivalent", "allowed-action-exists")
SPEC = {"semantics": SEMANTICS, "encoder": BUILD,
        "domain": "declared types; mathematical integers; Unicode scalar sequences; extensional finite sets",
        "queries": ["D", "D and not (AST-defined and VM-defined and AST=VM)", "D and AST-defined and AST"],
        "scope": "ideal pure semantics on one immutable input, conditional on sufficient evaluation work; no fuel-bound, state-transition or external-effect proof",
        "runtime_budget_encoded": False}


class EncodingLimit(Exception):
    pass


def _shape(t):
    """SMT sorts erase refinements; domain constraints retain every bound."""
    if t.kind == "Record":
        return (t.kind, tuple((k, _shape(v)) for k, v in sorted(t.fields)))
    if t.kind in {"Option", "Set"}:
        return (t.kind, _shape(t.element))
    return (t.kind, t.name, t.members)


class _Encoding:
    def __init__(self, z3, predicate, max_steps):
        self.z = z3
        self.ctx = z3.Context()
        self.sorts = {}
        self.serial = 0
        self.steps = 0
        self.max_steps = max_steps
        self.fields = {k: (t, z3.Const("input_" + str(i), self.sort(t)))
                       for i, (k, t) in enumerate(predicate.fields)}

    def tick(self):
        self.steps += 1
        if self.steps > self.max_steps:
            raise EncodingLimit("encoding_step_limit")

    def boolean(self, value):
        return self.z.BoolVal(value, self.ctx)

    def all(self, values):
        values = list(values)
        return self.z.And(*values) if values else self.boolean(True)

    def any(self, values):
        values = list(values)
        return self.z.Or(*values) if values else self.boolean(False)

    def index(self):
        self.serial += 1
        return self.z.Int("index_" + str(self.serial), self.ctx)

    def sort(self, t):
        self.tick()
        key = _shape(t)
        if key in self.sorts:
            return self.sorts[key]
        z = self.z
        if t.kind == "Bool":
            sort = z.BoolSort(self.ctx)
        elif t.kind in {"Int", "Enum"}:
            sort = z.IntSort(self.ctx)
        elif t.kind in {"String", "Id"}:
            # Seq(Int), not Z3 String: include ALL Python Unicode scalar values.
            sort = z.SeqSort(z.IntSort(self.ctx))
        elif t.kind == "Set":
            sort = z.SeqSort(self.sort(t.element))
        else:
            name = "type_" + sha256(repr(key).encode()).hexdigest()
            datatype = z.Datatype(name, self.ctx)
            if t.kind == "Record":
                datatype.declare("record_" + name, *[("field_" + str(i), self.sort(v))
                                  for i, (_, v) in enumerate(sorted(t.fields))])
            elif t.kind == "Option":
                datatype.declare("none_" + name)
                datatype.declare("some_" + name, ("value_" + name, self.sort(t.element)))
            else:
                raise EncodingLimit("unsupported_type:" + t.kind)
            sort = datatype.create()
        self.sorts[key] = sort
        return sort

    def domain(self, t, value):
        self.tick()
        z = self.z
        if t.kind == "Bool":
            return self.boolean(True)
        if t.kind == "Int":
            return z.And(value >= t.lower, value <= t.upper)
        if t.kind == "Enum":
            return z.And(value >= 0, value < len(t.members))
        if t.kind == "Record":
            return self.all(self.domain(v, self.sort(t).accessor(0, i)(value))
                            for i, (_, v) in enumerate(sorted(t.fields)))
        if t.kind == "Option":
            s = self.sort(t)
            return z.Implies(s.recognizer(1)(value), self.domain(t.element, s.accessor(1, 0)(value)))
        if t.kind == "Set":
            return z.And(z.Length(value) <= t.max_size,
                         self.all(z.Implies(i < z.Length(value), self.domain(t.element, value[i]))
                                  for i in range(t.max_size)))
        i = self.index()
        if t.kind in {"String", "Id"}:
            char = value[i]
            valid = z.And(char >= 0, char <= 0x10FFFF, z.Or(char < 0xD800, char > 0xDFFF))
            maximum, minimum = 256, int(t.kind == "Id")
        else:
            raise EncodingLimit("unsupported_domain:" + t.kind)
        return z.And(z.Length(value) >= minimum, z.Length(value) <= maximum,
                     z.ForAll([i], z.Implies(z.And(i >= 0, i < z.Length(value)), valid)))

    def literal(self, t, value):
        self.tick()
        z = self.z
        if t.kind == "Bool":
            return self.boolean(value)
        if t.kind == "Int":
            return z.IntVal(value, self.ctx)
        if t.kind == "Enum":
            return z.IntVal(t.members.index(value.value), self.ctx)
        if t.kind == "Record":
            return self.sort(t).constructor(0)(*[self.literal(v, value.get(k)) for k, v in sorted(t.fields)])
        if t.kind == "Option":
            s = self.sort(t)
            return s.constructor(0)() if value is None else s.constructor(1)(self.literal(t.element, value.value))
        if t.kind in {"String", "Id"}:
            sequence = [z.IntVal(ord(c), self.ctx) for c in (value.value if t.kind == "Id" else value)]
        elif t.kind == "Set":
            ordered = sorted(value, key=lambda v: canonical_bytes(t.element.value_wire(v)))
            sequence = [self.literal(t.element, v) for v in ordered]
        else:
            raise EncodingLimit("unsupported_literal:" + t.kind)
        return z.Concat(*[z.Unit(v) for v in sequence]) if len(sequence) > 1 else (
            z.Unit(sequence[0]) if sequence else z.Empty(self.sort(t)))

    def member(self, t, value, sequence, collection_type):
        self.tick()
        z = self.z
        return self.any(z.And(i < z.Length(sequence), self.equal(t, value, sequence[i], collection_type.element))
                        for i in range(collection_type.max_size))

    def equal(self, t, a, b, other_type=None):
        self.tick()
        z = self.z
        other_type = t if other_type is None else other_type
        if t.kind == "Set":
            # Sequence order and duplicate representatives have no set meaning.
            return z.And(self.all(z.Implies(i < z.Length(a), self.member(t.element, a[i], b, other_type))
                                  for i in range(t.max_size)),
                         self.all(z.Implies(j < z.Length(b), self.member(other_type.element, b[j], a, t))
                                  for j in range(other_type.max_size)))
        if t.kind == "Record":
            s = self.sort(t)
            return self.all(self.equal(v, s.accessor(0, i)(a), s.accessor(0, i)(b), dict(other_type.fields)[k])
                            for i, (k, v) in enumerate(sorted(t.fields)))
        if t.kind == "Option":
            s = self.sort(t)
            return z.And(s.recognizer(1)(a) == s.recognizer(1)(b),
                         z.Implies(s.recognizer(1)(a), self.equal(t.element, s.accessor(1, 0)(a), s.accessor(1, 0)(b), other_type.element)))
        return a == b

    def get(self, t, value, field):
        for i, (k, v) in enumerate(sorted(t.fields)):
            if k == field:
                return v, self.sort(t).accessor(0, i)(value)
        raise EncodingLimit("invalid_record_projection")

    def ast(self, node):
        self.tick()
        z = self.z
        tag = node.tag
        if tag == "Const":
            return node.type, self.literal(node.type, node.value), self.boolean(True)
        if tag == "Field":
            return *self.fields[node.value], self.boolean(True)
        args = [self.ast(c) for c in node.children]
        t, a, defined = args[0]
        if tag == "Get":
            return *self.get(t, a, node.value), defined
        if tag == "Not":
            value = z.Not(a)
        elif tag == "IsSome":
            value = self.sort(t).recognizer(1)(a)
        elif tag == "Unwrap":
            value = self.sort(t).accessor(1, 0)(a)
            defined = z.And(defined, self.sort(t).recognizer(1)(a))
        elif tag == "If":
            value = z.If(a, args[1][1], args[2][1])
            defined = z.And(defined, z.If(a, args[1][2], args[2][2]))
        else:
            tb, b, db = args[1]
            if tag == "And":
                value, defined = z.And(a, b), z.And(defined, z.Implies(a, db))
            elif tag == "Or":
                value, defined = z.Or(a, b), z.And(defined, z.Implies(z.Not(a), db))
            else:
                defined = z.And(defined, db)
                if tag == "Eq": value = self.equal(t, a, b, tb)
                elif tag == "Le": value = a <= b
                elif tag == "Lt": value = a < b
                elif tag == "Add": value = a + b
                elif tag == "Sub": value = a - b
                elif tag == "MulConst": value = a * b
                elif tag == "InSet": value = self.member(t, a, b, tb)
                else: raise EncodingLimit("unsupported_node:" + tag)
        return node.type, value, defined

    def vm(self, checks):
        """Symbolically execute actual forward bytecode, including stack paths."""
        z = self.z
        pending = [(0, (), self.boolean(True))]
        endings = []
        while pending:
            self.tick()
            pc, saved, path = pending.pop()
            stack = list(saved)
            path = z.simplify(path)
            if z.is_false(path):
                continue
            if pc == len(checks.instructions):
                if len(stack) != 1 or stack[0][0].kind != "Bool":
                    raise EncodingLimit("invalid_vm_result")
                endings.append((path, stack[0][1]))
                continue
            inst = checks.instructions[pc]
            op, target = inst.op, pc + 1
            if op == "PUSH": stack.append((inst.type, self.literal(inst.type, inst.arg)))
            elif op == "LOAD": stack.append(self.fields[inst.arg])
            elif op == "GET":
                t, a = stack.pop(); stack.append(self.get(t, a, inst.arg))
            elif op == "NOT":
                _, a = stack.pop(); stack.append((BOOL, z.Not(a)))
            elif op == "IS_SOME":
                t, a = stack.pop(); stack.append((BOOL, self.sort(t).recognizer(1)(a)))
            elif op == "UNWRAP":
                t, a = stack.pop()
                path = z.And(path, self.sort(t).recognizer(1)(a))
                stack.append((t.element, self.sort(t).accessor(1, 0)(a)))
            elif op in {"JF_KEEP", "JT_KEEP", "JF_POP", "JUMP"}:
                if type(inst.arg) is not int or not pc < inst.arg <= len(checks.instructions):
                    raise EncodingLimit("non_forward_vm_jump")
                if op == "JUMP": target = inst.arg
                else:
                    _, a = stack[-1]
                    take = a if op == "JT_KEEP" else z.Not(a)
                    jump_stack = stack if op != "JF_POP" else stack[:-1]
                    pending.append((inst.arg, tuple(jump_stack), z.And(path, take)))
                    stack.pop(); path = z.And(path, z.Not(take))
            else:
                tb, b = stack.pop(); t, a = stack.pop()
                if op == "EQ": value = self.equal(t, a, b, tb)
                elif op == "LE": value = a <= b
                elif op == "LT": value = a < b
                elif op == "ADD": value = a + b
                elif op == "SUB": value = a - b
                elif op == "MUL": value = a * b
                elif op == "IN": value = self.member(t, a, b, tb)
                else: raise EncodingLimit("unsupported_instruction:" + op)
                stack.append((FieldType("Int") if op in {"ADD", "SUB", "MUL"} else BOOL, value))
            pending.append((target, tuple(stack), path))
        return self.any(z.And(p, value) for p, value in endings), self.any(p for p, _ in endings)

    def decode(self, model, t, value, budget):
        budget[0] -= 1
        if budget[0] < 0:
            raise EncodingLimit("witness_size_limit")
        z = self.z
        if t.kind == "Bool": return z.is_true(model.eval(value, model_completion=True))
        if t.kind == "Int": return model.eval(value, model_completion=True).as_long()
        if t.kind == "Enum": return Tagged(t.name, t.members[model.eval(value, model_completion=True).as_long()])
        if t.kind == "Record":
            return Record(tuple((k, self.decode(model, v, self.sort(t).accessor(0, i)(value), budget))
                                for i, (k, v) in enumerate(sorted(t.fields))))
        if t.kind == "Option":
            s = self.sort(t)
            if not z.is_true(model.eval(s.recognizer(1)(value), model_completion=True)): return None
            return Some(self.decode(model, t.element, s.accessor(1, 0)(value), budget))
        length = model.eval(z.Length(value), model_completion=True).as_long()
        if not 0 <= length <= (t.max_size if t.kind == "Set" else 256):
            raise EncodingLimit("invalid_model_length")
        if t.kind == "Set":
            return frozenset(self.decode(model, t.element, value[i], budget) for i in range(length))
        budget[0] -= length
        if budget[0] < 0: raise EncodingLimit("witness_size_limit")
        text = "".join(chr(model.eval(value[i], model_completion=True).as_long()) for i in range(length))
        return Tagged(t.name, text) if t.kind == "Id" else text


def _check(z3, solver):
    result = solver.check()
    if result == z3.sat: return "Sat", None
    if result == z3.unsat: return "Unsat", None
    reason = solver.reason_unknown()
    return ("Timeout" if "timeout" in reason.lower() else "Unknown"), reason


def verify_smt(predicate, *, timeout_ms=5000, rlimit=1000000, max_encoding_steps=50000,evaluation_limit=DEFAULT_WORK_LIMIT):
    """Revalidate IR, regenerate checks, then emit separately scoped local VCs."""
    checks = compile_checks(predicate)
    return _verify_checks(checks, timeout_ms=timeout_ms, rlimit=rlimit, max_encoding_steps=max_encoding_steps,evaluation_limit=evaluation_limit)


def _verify_checks(checks, *, timeout_ms=5000, rlimit=1000000, max_encoding_steps=50000,evaluation_limit=DEFAULT_WORK_LIMIT):
    """Internal differential-test seam; public entry never trusts supplied code."""
    for value, maximum in ((timeout_ms, 60000), (rlimit, 100000000), (max_encoding_steps, 1000000)):
        if type(value) is not int or not 1 <= value <= maximum:
            raise Reject("INVALID_SMT_LIMIT")
    p = checks.predicate
    WorkBudget(evaluation_limit)
    bounds = {"fields": {k: t.wire() for k, t in p.fields}, "timeout_ms": str(timeout_ms),
              "rlimit": str(rlimit), "max_encoding_steps": str(max_encoding_steps), "max_witness_nodes": "10000",
              "evaluation_work_model":WORK_MODEL,"evaluation_work_limit":str(evaluation_limit),"runtime_budget_encoded":False}
    base = {"requirement_ids": [p.requirement_id], "ir_digest": p.semantic_digest,
            "source_digest": p.source_digest, "spec_digest": digest(SPEC, BUILD + "/spec"),
            "checker_build": COMPILER, "encoder_build": BUILD,
            "checks_sha256": sha256(checks.to_json().encode()).hexdigest(),
            "assumption_ids": ["pure-ir-typing", "smt-value-encoding", "smt-ast-encoding", "smt-vm-encoding",
                               "python-runtime", "z3-" + Z3_PACKAGE,"evaluation-work-sufficient"],
            "tool_versions": {"python": platform.python_version(), "z3_package": None, "z3_core": None},
            "bounds": bounds, "runtime_obligations": ["enforce-evaluation-work-budget"], "checked_at": datetime.now(timezone.utc).isoformat(),
            "scope": SPEC["scope"], "independent_certificate_checked": False}
    reports, queries = [], []
    bundle = {"schema": BUILD, "reports": reports, "queries": queries}

    def report(prop, status, solver_status, **extra):
        reports.append({**base, "property_id": prop, "status": status, "solver_status": solver_status,
                        "witness_ref": None, **extra})

    try:
        import z3
        base["tool_versions"].update(z3_package=version("z3-solver"), z3_core=z3.get_version_string())
        if version("z3-solver") != Z3_PACKAGE or z3.get_version_string() != Z3_CORE:
            raise EncodingLimit("solver_version_mismatch")
    except (ImportError, PackageNotFoundError, EncodingLimit) as exc:
        for prop in PROPERTIES: report(prop, "Unknown", "Error", reason=str(exc))
        return bundle
    try:
        enc = _Encoding(z3, p, max_encoding_steps)
        domain = enc.all(enc.domain(t, value) for t, value in enc.fields.values())
        _, ast, ast_defined = enc.ast(p.root)
        vm, vm_defined = enc.vm(checks)
        formulas = [domain, z3.And(domain, z3.Not(z3.And(ast_defined, vm_defined, ast == vm))),
                    z3.And(domain, ast_defined, ast)]
    except (EncodingLimit, z3.Z3Exception, ValueError, TypeError, KeyError, IndexError, RecursionError) as exc:
        for prop in PROPERTIES: report(prop, "Unknown", "Error", reason="encoding_failed:" + str(exc))
        return bundle
    domain_ok = False
    for prop, formula in zip(PROPERTIES, formulas):
        solver = z3.Solver(ctx=enc.ctx)
        solver.set(timeout=timeout_ms, rlimit=rlimit, random_seed=0)
        solver.add(formula)
        query = (f"(set-option :timeout {timeout_ms})\n(set-option :rlimit {rlimit})\n"
                 "(set-option :smt.random_seed 0)\n" + solver.to_smt2())
        query_hash = sha256(query.encode()).hexdigest()
        item = {"property_id": prop, "query_sha256": query_hash, "smt2": query}
        queries.append(item)
        extra = {"query_sha256": query_hash}
        try:
            solver_status, reason = _check(z3, solver)
            item.update(solver_status=solver_status, reason=reason, statistics=str(solver.statistics()))
            status = "Unknown"
            if solver_status == "Sat":
                model = solver.model()
                item["model"] = model.sexpr()
                budget = [10000]
                values = p.values({k: enc.decode(model, t, value, budget) for k, (t, value) in enc.fields.items()})
                witness = {k: t.value_wire(values[k]) for k, t in p.fields}
                exhausted=[]
                def run(fn):
                    try: return {"value": fn(values,budget=WorkBudget(evaluation_limit)), "error": None}
                    except EvaluationLimit as exc:
                        exhausted.append(True)
                        return {"value":None,"error":"EvaluationLimit:"+exc.code}
                    except (Reject, AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
                        return {"value": None, "error": type(exc).__name__ + ":" + str(exc)}
                replay = {"ast": run(p.evaluate), "checks": run(checks.evaluate)}
                extra.update(witness=witness, witness_ref="#/queries/" + str(len(queries) - 1) + "/witness", replay=replay)
                item.update(witness=witness, replay=replay)
                if prop == "input-domain-inhabited":
                    domain_ok = True; status = "ProvedUnder"
                elif exhausted:
                    reason="evaluation_work_exhausted_during_replay"
                elif prop == "checks-equivalent":
                    if replay["ast"] != replay["checks"] or any(v["error"] for v in replay.values()): status = "Violated"
                    else: reason = "countermodel_did_not_replay"
                elif all(v == {"value": True, "error": None} for v in replay.values()): status = "ProvedUnder"
                else: reason = "allowed_witness_did_not_replay"
            elif solver_status == "Unsat":
                if prop == "input-domain-inhabited": status = "AssumptionBroken"
                elif prop == "checks-equivalent" and domain_ok: status = "ProvedUnder"
                elif prop == "allowed-action-exists" and domain_ok: status = "Violated"
            if prop != "input-domain-inhabited" and not domain_ok:
                status, reason = "Unknown", "input_domain_not_confirmed"
            report(prop, status, solver_status, reason=reason, **extra)
        except (EncodingLimit, z3.Z3Exception, Reject, ValueError, TypeError, KeyError, IndexError, RecursionError) as exc:
            # A Sat whose model cannot be decoded/replayed is still Sat, not a proof.
            report(prop, "Unknown", item.get("solver_status", "Error"), reason="query_or_replay_failed:" + str(exc), **extra)
    bundle["encoding_steps"] = str(enc.steps)
    return bundle
