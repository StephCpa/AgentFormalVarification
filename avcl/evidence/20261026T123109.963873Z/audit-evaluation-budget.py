"""Reproduce the work examples and audit a completed frozen verification run."""
import hashlib
import json
from pathlib import Path
import re
import sys

HERE=Path(__file__).resolve().parent
ROOT=HERE.parent.parent
sys.path.insert(0,str(ROOT))
from avcl.canonical import canonical_bytes
from avcl.checks import compile_checks
from avcl.ir import compile_predicate
from avcl.ir_types import FieldType,Record
from avcl.evaluation import WorkBudget,EvaluationLimit,DEFAULT_WORK_LIMIT,MAX_WORK_LIMIT,WORK_MODEL

verification=json.loads((HERE/"verification.json").read_text(encoding="utf-8"))
mismatches=[name for name,expected in verification["source_sha256"].items()
            if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=expected]
counts={}
for stage in verification["stages"]:
    match=re.search(r"^Ran (\d+) tests? in",(HERE/stage["log"]).read_text(encoding="utf-8"),re.M)
    if match:counts[stage["name"]]=int(match[1])

flat_record=FieldType("Record",fields=tuple(("f"+str(k),FieldType("String")) for k in range(8)))
flat_type=FieldType("Set",element=flat_record)
flat=frozenset(Record(tuple(("f"+str(k),str(i).zfill(2)+"x"*30) for k in range(8))) for i in range(64))
nested_record=FieldType("Record",fields=(("f0",FieldType("String")),("f1",FieldType("String"))))
nested_type=FieldType("Set",element=FieldType("Set",element=nested_record,max_size=4),max_size=64)
nested=frozenset(frozenset(Record(tuple(("f"+str(k),f"{i:02}{j:02}"+"x"*28) for k in range(2)))
                           for j in range(4)) for i in range(64))
cases=[]
for name,t,value in (("flat-record-set",flat_type,flat),("nested-record-set",nested_type,nested)):
    p=compile_predicate({"tag":"Eq","args":[{"tag":"Field","name":"a"},{"tag":"Field","name":"b"}]},{"a":t,"b":t})
    env={"a":value,"b":value}
    encoded=canonical_bytes({k:t.value_wire(v) for k,v in env.items()})
    assert len(encoded)<65536
    for limit in (DEFAULT_WORK_LIMIT,MAX_WORK_LIMIT):
        row={"case":name,"input_wire_bytes":len(encoded),"ast_nodes":3,"work_limit":limit,"backends":{}}
        for backend,runner in (("ast",p),("checks",compile_checks(p))):
            budget=WorkBudget(limit)
            try:
                answer=runner.evaluate(env,budget=budget)
                result={"status":"RuntimeSatisfied" if answer else "Violated","value":answer,"error":None}
            except EvaluationLimit as exc:
                result={"status":"Unknown","value":None,"error":exc.code}
            row["backends"][backend]={**result,"work":budget.report()}
        assert row["backends"]["ast"]==row["backends"]["checks"]
        expected="Unknown" if name=="nested-record-set" and limit==DEFAULT_WORK_LIMIT else "RuntimeSatisfied"
        assert row["backends"]["ast"]["status"]==expected
        cases.append(row)
m1=json.loads((HERE/"m1-targeted/report.json").read_text(encoding="utf-8"))
durable=[case for case in m1["cases"] if case["test_id"].startswith("test_evaluation_budget.")]
assert len(durable)==2 and all(case["status"]=="Passed" and case["observations"]["durable_snapshot_unchanged"] for case in durable)
result={"schema":"avcl/evaluation-budget-audit/0.1","verification_passed":verification["passed"],
        "source_files_checked":len(verification["source_sha256"]),"source_hash_mismatches":mismatches,
        "design_baselines_unchanged":all(d["unchanged"] for d in verification["design_baselines"]),
        "stage_count":len(verification["stages"]),"test_counts":counts,"work_model":WORK_MODEL,
        "pure_cases":cases,"durable_atomic_failures":durable,
        "m1_release_decision":"not_issued",
        "limits":["conservative abstract work units, not wall-clock or whole-service bounds",
                  "SMT fuel semantics not encoded; resource-sufficiency assumption explicit",
                  "full-design requirement audit, durable denial logs and extension diagnostic review remain"]}
payload=json.dumps(result,ensure_ascii=False,indent=2)+"\n"
destination=HERE/"evaluation-budget-audit.json"
if destination.exists():
    if destination.read_text(encoding="utf-8")!=payload:raise SystemExit("Existing frozen audit differs; retain it and use new evidence")
else:destination.write_text(payload,encoding="utf-8")
print(json.dumps({k:v for k,v in result.items() if k not in {"pure_cases","durable_atomic_failures"}},ensure_ascii=False,indent=2))
print("Cases: "+json.dumps([{k:v for k,v in row.items() if k!="backends"}|{"result":row["backends"]["ast"]} for row in cases]))
sys.exit(0 if verification["passed"] and not mismatches else 1)
