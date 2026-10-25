"""Start the local-only runtime API from a private host configuration file."""
from pathlib import Path
import argparse
import json
import os
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from avcl.gateway import RuntimeGateway
from avcl.http_gateway import LocalRuntimeServer

def main():
    parser=argparse.ArgumentParser();parser.add_argument("--config",required=True);parser.add_argument("--ready-file",required=True)
    args=parser.parse_args();config=json.loads(Path(args.config).read_text(encoding="utf-8"))
    if set(config)!={"dsn","schema","clock_file"}:raise ValueError("invalid local API configuration")
    def clock():
        value=json.loads(Path(config["clock_file"]).read_text(encoding="utf-8"))["now"]
        if type(value) is not int or value<0:raise ValueError("invalid logical test clock")
        return value
    gateway=RuntimeGateway(config["dsn"],config["schema"],clock)
    with LocalRuntimeServer(("127.0.0.1",0),gateway) as server:
        ready=Path(args.ready_file);temporary=ready.with_suffix(".pending")
        temporary.write_text(json.dumps({"host":"127.0.0.1","port":server.server_address[1],"pid":os.getpid()}),encoding="utf-8")
        temporary.replace(ready)
        server.serve_forever(poll_interval=0.1)

if __name__=="__main__":main()
