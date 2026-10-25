"""Child process used by real-database crash tests. Exit 97 is intentional."""
from pathlib import Path
import json
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from avcl.postgres import PostgresKernel
from avcl.storage_codec import loads
from avcl.supervision import PostgresSupervisor
from avcl.workflow_postgres import DurableWorkflowExecutor
from avcl.validation import validate_candidate
from avcl.canonical import strict_loads
from avcl.tasks import PostgresTasks

profile=json.loads((ROOT/".runtime"/"local-postgres.json").read_text(encoding="utf-8"))
job=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
k=PostgresKernel(profile["dsn"],schema=job["schema"],crash_at=job["crash_at"],clock=lambda:job.get("now",0))
args=loads(job["args"])
target=k.trusted if job["method"]=="observe" else k
method=job["method"]
if method.startswith("supervisor."):target=PostgresSupervisor(k);method=method.split(".",1)[1]
if method.startswith("workflow."):target=DurableWorkflowExecutor(k);method=method.split(".",1)[1]
if method.startswith("tasks."):target=PostgresTasks(k);method=method.split(".",1)[1]
if method=="validation":k._transaction("api-validate",lambda kernel,c:validate_candidate(kernel,c,args[0],strict_loads(args[1])))
else:getattr(target,method)(*args)
raise RuntimeError("Declared crash point was not reached")
