"""Measure normalization visits, not wall time or all Python work.

This read-only probe demonstrates why a three-node AST is not a three-unit
evaluation. It is not an implementation of the proposed work-budget gate.
"""
import json
from pathlib import Path
import sys
from unittest.mock import patch

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE.parent.parent))
from avcl.canonical import canonical_bytes
from avcl.ir import compile_predicate
from avcl.ir_types import FieldType,Record
from avcl.checks import compile_checks

record=FieldType("Record",fields=tuple(("f"+str(i),FieldType("String")) for i in range(8)))
collection=FieldType("Set",element=record,max_size=64)
p=compile_predicate({"tag":"Eq","args":[{"tag":"Field","name":"left"},{"tag":"Field","name":"right"}]},
                    {"left":collection,"right":collection})
checks=compile_checks(p)
original=FieldType.normalize
results=[]
for count in (1,8,32,64):
    def values():
        return frozenset(Record(tuple(("f"+str(j),str(i).zfill(2)+"x"*30) for j in range(8))) for i in range(count))
    env={"left":values(),"right":values()}
    wire=canonical_bytes({k:collection.value_wire(v) for k,v in env.items()})
    row={"set_elements":count,"input_wire_bytes":len(wire),"ast_nodes":3,"backends":{}}
    for name,evaluate in (("ast",p.evaluate),("checks",checks.evaluate)):
        visits={"normalization_calls":0,"string_characters":0}
        def measured(type_,value):
            visits["normalization_calls"]+=1
            if type_.kind=="String":visits["string_characters"]+=len(value)
            return original(type_,value)
        with patch.object(FieldType,"normalize",measured):
            answer=evaluate(env)
        assert answer is True
        row["backends"][name]={"result":answer,**visits}
    assert len(wire)<=65536
    results.append(row)
report={"scope":"normalization calls and string characters only; excludes hashing, equality and instruction work",
        "runtime_budget_implemented":False,"results":results}
(HERE/"evaluation-work-probe.json").write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
print(json.dumps(report,indent=2))
