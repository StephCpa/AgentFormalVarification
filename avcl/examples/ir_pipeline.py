"""Build real-runtime and finite-domain IR candidates, keeping Unknown visible."""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from avcl.ir import FieldType,compile_predicate,refund_predicate
from avcl.package import write_candidate,load_candidate
from avcl.verification import verify_finite

def c(v): return {"tag":"Const","value":v}
def f(name): return {"tag":"Field","name":name}
def call(tag,*args): return {"tag":tag,"args":list(args)}
def get(expr,name): return {"tag":"Get","args":[expr],"field":name}

def bounded_refund():
    currency=FieldType("Enum",name="Currency",members=("CNY","USD"))
    order=FieldType("Record",fields=(("amount",FieldType("Int",0,3)),("currency",currency)))
    schema={"order":order,"approval":FieldType("Option",element=FieldType("Int",0,3)),
            "allowed":FieldType("Set",element=currency,max_size=1)}
    amount=get(f("order"),"amount")
    expr=call("And",call("Lt",c(0),amount),call("And",
              call("InSet",get(f("order"),"currency"),f("allowed")),call("And",
              call("IsSome",f("approval")),call("Le",amount,call("Unwrap",f("approval"))))))
    return compile_predicate(expr,schema,"R13",source="examples/ir_pipeline.py:bounded_refund")

def main():
    if len(sys.argv)!=2: raise SystemExit("usage: python examples/ir_pipeline.py OUTPUT_DIRECTORY")
    root=Path(sys.argv[1]);root.mkdir(parents=True,exist_ok=False)
    summary=[]
    for name,predicate in [("runtime-refund",refund_predicate()),("bounded-refund",bounded_refund())]:
        destination=write_candidate(root/name,predicate)
        load_candidate(destination,predicate.semantic_digest)
        reports=verify_finite(predicate)
        summary.append({"name":name,"ir_digest":predicate.semantic_digest,"reports":reports})
    bounded=summary[1]["reports"]
    assert all(r["status"]=="BoundedChecked" and r["checked_cases"]=="120" for r in bounded)
    assert all(r["status"]=="Unknown" for r in summary[0]["reports"])
    print(json.dumps(summary,ensure_ascii=False,indent=2))

if __name__=="__main__": main()
