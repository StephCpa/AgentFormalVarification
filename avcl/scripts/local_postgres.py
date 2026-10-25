"""Workspace-owned PostgreSQL test cluster; never installs a Windows service."""
from pathlib import Path
import argparse
import json
import os
import secrets
import socket
import subprocess
import tempfile

ROOT=Path(__file__).resolve().parents[1]
RUNTIME=ROOT/".runtime"
PROFILE=RUNTIME/"local-postgres.json"
BIN=RUNTIME/"postgresql-18.6"/"pgsql"/"bin"
DATA=RUNTIME/"pg-data"

def run(name,*args,check=True):
    # PostgreSQL children inherit handles on Windows. Regular files avoid waiting
    # for pipe EOF after pg_ctl itself has already exited successfully.
    with tempfile.TemporaryFile() as output:
        result=subprocess.run([str(BIN/(name+".exe")),*map(str,args)],stdout=output,stderr=output,
            timeout=90,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        output.seek(0);result.stdout=output.read().decode("utf-8",errors="replace")
    if check and result.returncode: raise RuntimeError(name+" failed: "+result.stdout)
    return result

def main():
    parser=argparse.ArgumentParser();parser.add_argument("action",choices=["start","stop","status"],default="start",nargs="?")
    action=parser.parse_args().action
    if action!="start":
        if not PROFILE.exists(): raise RuntimeError("No workspace cluster profile")
        profile=json.loads(PROFILE.read_text(encoding="utf-8"))
        if Path(profile["data"]).resolve()!=DATA.resolve(): raise RuntimeError("Cluster ownership mismatch")
        result=run("pg_ctl","-D",DATA,"status",check=False)
        if action=="stop" and result.returncode==0: run("pg_ctl","-D",DATA,"-m","fast","-w","stop")
        print("Workspace PostgreSQL "+("stopped" if action=="stop" else "running" if result.returncode==0 else "stopped"));return
    if not (BIN/"postgres.exe").is_file(): raise RuntimeError("Run scripts/bootstrap_postgres.py first")
    if not PROFILE.exists():
        if DATA.exists(): raise RuntimeError("Unowned data directory; refusing to initialize")
        with socket.socket() as sock: sock.bind(("127.0.0.1",0));port=sock.getsockname()[1]
        password=secrets.token_hex(32);pwfile=RUNTIME/"initdb-password.tmp"
        pwfile.write_text(password,encoding="ascii")
        try: run("initdb","-D",DATA,"--encoding=UTF8","--locale=C","--auth=scram-sha-256","--username=avcl_admin","--pwfile="+str(pwfile))
        finally: pwfile.unlink(missing_ok=True)
        # This private test profile is deliberately excluded from evidence/manifests.
        PROFILE.write_text(json.dumps({"data":str(DATA.resolve()),"port":port,
            "dsn":f"host=127.0.0.1 port={port} dbname=postgres user=avcl_admin password={password}"}),encoding="utf-8")
    profile=json.loads(PROFILE.read_text(encoding="utf-8"))
    if Path(profile["data"]).resolve()!=DATA.resolve(): raise RuntimeError("Cluster ownership mismatch")
    if run("pg_ctl","-D",DATA,"status",check=False).returncode!=0:
        run("pg_ctl","-D",DATA,"-l",RUNTIME/"postgres.log","-o",f"-h 127.0.0.1 -p {profile['port']} -c fsync=on -c synchronous_commit=on -c full_page_writes=on","-w","start")
    print(run("postgres","--version").stdout.strip()+"; workspace cluster running on loopback")

if __name__=="__main__":main()
