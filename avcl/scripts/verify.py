"""Run the declared verification suite and freeze its evidence, including failures."""
from datetime import datetime, timezone
from pathlib import Path
import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def run_logged(command,log,cwd,timeout):
    """Stream evidence to disk; preserve prior output on failure or timeout."""
    with log.open("w",encoding="utf-8") as stream:
        try:
            result=subprocess.run(command,cwd=cwd,stdout=stream,stderr=subprocess.STDOUT,timeout=timeout)
            code=result.returncode
        except (OSError,subprocess.TimeoutExpired) as exc:
            code=-1
            stream.write("\nVERIFICATION_RUNNER_ERROR: "+str(exc)+"\n")
    return code,log.read_text(encoding="utf-8",errors="replace")


def check_lean_axioms(output,theorems):
    """Require actual dependency declarations, not incidental theorem mentions."""
    allowed={"propext","Quot.sound"};found={name:[] for name in theorems}
    pattern=re.compile(r"^'([^']+)' (?:depends on axioms: \[([^\]]*)\]|does not depend on any axioms)$")
    for line in output.splitlines():
        match=pattern.fullmatch(line.strip())
        if match and match[1] in found:
            found[match[1]].append([] if match[2] is None else [item.strip() for item in match[2].split(",") if item.strip()])
    missing=[name for name,records in found.items() if not records]
    duplicate=[name for name,records in found.items() if len(records)>1]
    dependencies={name:records[0] for name,records in found.items() if len(records)==1}
    unexpected={name:sorted(set(deps)-allowed) for name,deps in dependencies.items() if set(deps)-allowed}
    contains_sorry="sorryAx" in output or "declaration uses `sorry`" in output
    return {"passed":bool(theorems) and not (missing or duplicate or unexpected or contains_sorry),
            "theorems":dependencies,"missing":missing,"duplicate":duplicate,
            "unexpected_axioms":unexpected,"contains_sorry":contains_sorry}


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--postgres",action="store_true",help="Require real PostgreSQL integration checks")
    args=parser.parse_args()
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    out=ROOT/"evidence"/stamp
    out.mkdir(parents=True,exist_ok=False)
    stages=[]
    def run(name,command,cwd=ROOT,timeout=180):
        start=time.monotonic()
        code,output=run_logged(command,out/(name+".log"),cwd,timeout)
        stage={"name":name,"command":command,"exit_code":code,"passed":code==0,
               "seconds":round(time.monotonic()-start,3),"log":name+".log"}
        stages.append(stage)
        print(f"{name}: {'PASS' if stage['passed'] else 'FAIL'} ({stage['seconds']}s)",flush=True)
        return stage,output

    py=[sys.executable,"-X","utf8"]
    run("tests",py+["-m","unittest","discover","-v"])
    model,body=run("bounded-model",py+["-m","models.budget_model"])
    if model["passed"]:
        (out/"bounded-model.json").write_text(body,encoding="utf-8")
    capacity_model,body=run("capacity-model",py+["-m","models.capacity_model"])
    if capacity_model["passed"]:(out/"capacity-model.json").write_text(body,encoding="utf-8")
    delegation_model,body=run("delegation-model",py+["-m","models.delegation_model"])
    if delegation_model["passed"]:(out/"delegation-model.json").write_text(body,encoding="utf-8")
    cost_model,body=run("cost-bridge-model",py+["-m","models.cost_bridge_model"])
    if cost_model["passed"]:(out/"cost-bridge-model.json").write_text(body,encoding="utf-8")
    send_model,body=run("send-window-model",py+["-m","models.send_window_model"])
    if send_model["passed"]:(out/"send-window-model.json").write_text(body,encoding="utf-8")
    version,body=run("lean-version",["lean","--version"],ROOT/"formal"/"lean")
    version["passed"]=version["passed"] and "version 4.33.0" in body
    proof,body=run("lean-core",["lean","Core.lean"],ROOT/"formal"/"lean")
    proof["proof_gate"]=check_lean_axioms(body,("AVCL.P3_budget_preserved","AVCL.P1_P2_admission"))
    proof["passed"]=proof["passed"] and proof["proof_gate"]["passed"]
    delegation_proof,body=run("lean-delegation",["lean","Delegation.lean"],ROOT/"formal"/"lean")
    delegation_names=("P7_scope_transitive","P7_ancestor_revocation","P7_shared_budget","P7_composition",
                      "example_nonempty_run","example_rejects_second_reservation","example_partial_then_final",
                      "example_can_admit","example_root_revoked_leaf_not_revoked")
    delegation_proof["proof_gate"]=check_lean_axioms(body,tuple("AVCL.Delegation."+name for name in delegation_names))
    delegation_proof["passed"]=delegation_proof["passed"] and delegation_proof["proof_gate"]["passed"]
    cost_proof,body=run("lean-cost-bridge",["lean","CostBridge.lean"],ROOT/"formal"/"lean")
    cost_names=("P4_cost_bridge","P4_actual_within_budget","step_valid",
                "example_partial_then_final","example_local_budget_insufficient","example_closed_blocks_charge")
    cost_proof["proof_gate"]=check_lean_axioms(body,tuple("AVCL.CostBridge."+name for name in cost_names))
    cost_proof["passed"]=cost_proof["passed"] and cost_proof["proof_gate"]["passed"]
    run("refund-demo",py+["examples/refund_demo.py"])
    run("send-envelope-demo",py+["examples/send_envelope_demo.py",str(out/"send-envelope-demo")])
    run("ir-pipeline",py+["examples/ir_pipeline.py",str(out/"ir-candidates")])
    run("policy-lifecycle",py+["examples/policy_lifecycle.py",str(out/"policy-lifecycle")])
    run("workflow-demo",py+["examples/workflow_demo.py",str(out/"workflow-demo")])
    if args.postgres:
        environment,_=run("postgres-environment",py+["scripts/postgres_environment.py"])
        if environment["passed"]:
            shutil.copyfile(ROOT/".runtime"/"postgresql-18.6"/"toolchain-manifest.json",out/"postgres-toolchain-manifest.json")
        run("postgres-integration",py+["-m","unittest","discover","-s","integration","-v"],timeout=600)
        run("supervision-demo",py+["examples/supervision_demo.py",str(out/"supervision-demo")])
        run("durable-workflow-demo",py+["examples/durable_workflow_demo.py",str(out/"durable-workflow-demo")])
        run("close-workflow-demo",py+["examples/close_workflow_demo.py",str(out/"close-workflow-demo")])
        run("delegation-demo",py+["examples/delegation_demo.py",str(out/"delegation-demo")])
    baseline={
        "Agent形式化验证语言研究与设计.docx":"0543471efc21c36a0cbfcf210a1c0fe9be9c111bb894152d32cac00722b8e00a",
        "AVCL形式化验证语言与执行系统详细设计_v0.1.docx":"8fa7491cb6c428ccab6158e95dce23ec222e9250e3ca518f90b7bc1227dc34fc"}
    documents=[]
    for name,expected in baseline.items():
        path=ROOT.parent/name
        actual=sha(path) if path.is_file() else None
        documents.append({"path":name,"sha256":actual,"expected_sha256":expected,"unchanged":actual==expected})
    manifest={}
    for directory,dirs,files in os.walk(ROOT):
        dirs[:]=sorted(d for d in dirs if d not in {"evidence","__pycache__",".venv",".git",".lake",".runtime"})
        for name in sorted(files):
            path=Path(directory)/name;relative=path.relative_to(ROOT)
            if path.suffix in {".py",".lean",".toml",".md",".cmd",".json",".sql",".txt"} or path.name in {"lean-toolchain",".gitignore"}:
                manifest[relative.as_posix()]=sha(path)
    passed=all(s["passed"] for s in stages) and all(d["unchanged"] for d in documents)
    report={"schema":"avcl/verification/0.1","started_utc":stamp,"passed":passed,
            "scope":"reference semantics; bounded models; abstract Lean admission/budget, conditional cost bridge and delegation cores; "+("real PostgreSQL persistence and application process crash tests; " if args.postgres else "PostgreSQL not tested; ")+"no production guarantee",
            "postgres_requested":args.postgres,
            "python":sys.version,"platform":platform.platform(),"stages":stages,
            "design_baselines":documents,"source_sha256":manifest}
    (out/"verification.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(f"Evidence: {out}")
    return 0 if passed else 1


if __name__=="__main__": sys.exit(main())
