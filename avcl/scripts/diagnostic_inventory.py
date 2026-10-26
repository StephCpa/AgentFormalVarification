"""Freeze reviewed literal diagnostic sites; dynamic sites remain explicit."""
import ast
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from avcl.requirements import CODE_REQUIREMENTS


def literals(expr):
    if isinstance(expr,ast.Constant) and type(expr.value) is str:return [expr.value]
    if isinstance(expr,ast.IfExp):return literals(expr.body)+literals(expr.orelse)
    return []


def main(destination):
    sites=[];dynamic=[];hashes={}
    for path in sorted((ROOT/"avcl").glob("*.py")):
        relative=path.relative_to(ROOT).as_posix()
        hashes[relative]=hashlib.sha256(path.read_bytes()).hexdigest()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and
                    node.func.id in {"Reject","Diagnostic","WorkflowDiagnostic","error","fail"} and node.args):continue
            codes=literals(node.args[0]);site={"path":relative,"line":node.lineno,"callee":node.func.id}
            if not codes:dynamic.append({**site,"expression":ast.unparse(node.args[0])})
            for code in codes:
                sites.append({**site,"code":code,"reviewed":code in CODE_REQUIREMENTS,
                              "requirement_ids":list(CODE_REQUIREMENTS.get(code,()))})
    missing=sorted({s["code"] for s in sites if not s["reviewed"]})
    report={"format":"avcl/diagnostic-inventory/0.1","passed":not missing,
            "scope":"Literal and conditional-literal first arguments at named calls in avcl/*.py; not whole-program coverage or proof",
            "reviewed_codes":len(CODE_REQUIREMENTS),"observed_literal_codes":len({s["code"] for s in sites}),
            "missing_codes":missing,"sites":sites,"dynamic_sites":dynamic,"source_sha256":hashes}
    Path(destination).write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({k:report[k] for k in ("passed","reviewed_codes","observed_literal_codes","missing_codes")}))
    return 0 if report["passed"] else 1


if __name__=="__main__":sys.exit(main(sys.argv[1]))
