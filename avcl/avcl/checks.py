"""Deterministic stack checks generated from IR; no eval or executable source.

Loading regenerates the expected bytecode and compares all content. A matching
digest alone never authorizes arbitrary instructions. Authentication of policy
publishers remains a separate, unimplemented deployment requirement.
"""
from dataclasses import dataclass
from .canonical import Reject,canonical_bytes,strict_loads
from .ir import Predicate,Diagnostic,load_predicate

COMPILER="avcl/checks/0.2"

@dataclass(frozen=True)
class Instruction:
    op: str
    node_id: str
    arg: object=None
    type: object=None

    def wire(self):
        out={"op":self.op,"node_id":self.node_id}
        if self.op=="PUSH": out.update(type=self.type.wire(),value=self.type.value_wire(self.arg))
        elif self.arg is not None: out["arg"]=str(self.arg)
        return out

@dataclass(frozen=True)
class Checks:
    predicate: Predicate
    instructions: tuple

    def to_json(self):
        return canonical_bytes({"compiler":COMPILER,"ir_digest":self.predicate.semantic_digest,
            "requirement_id":self.predicate.requirement_id,"instructions":[i.wire() for i in self.instructions]}).decode("utf-8")

    def evaluate(self,values):
        env=self.predicate.values(values);stack=[];pc=0
        while pc<len(self.instructions):
            inst=self.instructions[pc];pc+=1;op=inst.op
            if op=="PUSH": stack.append(inst.arg)
            elif op=="LOAD": stack.append(env[inst.arg])
            elif op=="GET": stack.append(stack.pop().get(inst.arg))
            elif op=="NOT": stack.append(not stack.pop())
            elif op=="IS_SOME": stack.append(stack.pop() is not None)
            elif op=="UNWRAP": stack.append(stack.pop().value)
            elif op in {"JF_KEEP","JT_KEEP"}:
                if stack[-1] == (op=="JT_KEEP"): pc=inst.arg
                else: stack.pop()
            elif op=="JF_POP":
                if not stack.pop(): pc=inst.arg
            elif op=="JUMP": pc=inst.arg
            elif op in {"EQ","LE","LT","ADD","SUB","MUL","IN"}:
                b=stack.pop();a=stack.pop()
                if op=="EQ": result=a==b
                elif op=="LE": result=a<=b
                elif op=="LT": result=a<b
                elif op=="ADD": result=a+b
                elif op=="SUB": result=a-b
                elif op=="MUL": result=a*b
                else: result=a in b
                stack.append(result)
            else: raise Diagnostic("INVALID_CHECK_INSTRUCTION",self.predicate.requirement_id,inst.node_id)
        if len(stack)!=1 or type(stack[0]) is not bool:
            raise Diagnostic("INVALID_CHECK_RESULT",self.predicate.requirement_id,"n0")
        return stack[0]

def compile_checks(predicate):
    # Revalidate even programmatic IR instances; frozen dataclasses are not a
    # cryptographic authority boundary and callers may construct them directly.
    predicate=load_predicate(predicate.to_json())
    code=[]
    def emit(op,node,arg=None,type=None):
        index=len(code);code.append(Instruction(op,node.node_id,arg,type));return index
    def patch(index,target):
        old=code[index];code[index]=Instruction(old.op,old.node_id,target,old.type)
    def build(n):
        if n.tag=="Const": emit("PUSH",n,n.value,n.type)
        elif n.tag=="Field": emit("LOAD",n,n.value)
        elif n.tag in {"And","Or"}:
            build(n.children[0]);jump=emit("JF_KEEP" if n.tag=="And" else "JT_KEEP",n)
            build(n.children[1]);patch(jump,len(code))
        elif n.tag=="If":
            build(n.children[0]);branch=emit("JF_POP",n);build(n.children[1]);end=emit("JUMP",n)
            patch(branch,len(code));build(n.children[2]);patch(end,len(code))
        else:
            for child in n.children: build(child)
            ops={"Get":"GET","Not":"NOT","IsSome":"IS_SOME","Unwrap":"UNWRAP", "Eq":"EQ",
                 "Le":"LE","Lt":"LT","Add":"ADD","Sub":"SUB","MulConst":"MUL","InSet":"IN"}
            emit(ops[n.tag],n,n.value if n.tag=="Get" else None)
    build(predicate.root)
    return Checks(predicate,tuple(code))

def load_checks(raw,predicate):
    supplied=strict_loads(raw)
    expected=compile_checks(predicate)
    if supplied!=strict_loads(expected.to_json()): raise Reject("CHECK_ARTIFACT_MISMATCH")
    return expected
