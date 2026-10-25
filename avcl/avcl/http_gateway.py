"""Local integration transport. Explicitly refuses remote binds and browser origins."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from uuid import uuid4
from .canonical import canonical_bytes
from .gateway import RuntimeGateway

class LocalRuntimeServer(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=False
    def __init__(self,address,gateway):
        if address[0]!="127.0.0.1":raise ValueError("This transport only supports IPv4 loopback")
        self.gateway=gateway;super().__init__(address,Handler)

class Handler(BaseHTTPRequestHandler):
    protocol_version="HTTP/1.0"
    server_version="AVCL-Local/0.1"
    sys_version=""
    def setup(self):super().setup();self.connection.settimeout(5)
    def log_message(self,*args):pass  # Do not log request bodies/credentials.
    def send_error(self,code,message=None,explain=None):
        self.reply(code,RuntimeGateway.error("INVALID_HTTP_REQUEST","trace-"+uuid4().hex))
    def reply(self,status,payload):
        data=canonical_bytes(payload)
        self.send_response(status);self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(data)));self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff");self.send_header("Connection","close");self.end_headers()
        self.wfile.write(data);self.close_connection=True
    def perform(self):
        def reject(status,code):self.reply(status,RuntimeGateway.error(code,"trace-"+uuid4().hex))
        try:
            for header in ("Authorization","Host","Content-Length","Content-Type"):
                if len(self.headers.get_all(header,[]))>1:return reject(400,"DUPLICATE_HTTP_HEADER")
            expected="127.0.0.1:"+str(self.server.server_address[1])
            if self.headers.get("Host")!=expected or self.headers.get("Origin") is not None:return reject(403,"HTTP_ORIGIN_DENIED")
            if self.headers.get("Transfer-Encoding") is not None:return reject(400,"UNSUPPORTED_HTTP_ENCODING")
            raw_length=self.headers.get("Content-Length","0")
            if not raw_length.isascii() or not raw_length.isdecimal() or len(raw_length)>6:return reject(400,"INVALID_CONTENT_LENGTH")
            length=int(raw_length)
            if length>65536:return reject(413,"REQUEST_TOO_LARGE")
            if self.command=="POST" and self.headers.get("Content-Type")!="application/json":return reject(400,"INVALID_CONTENT_TYPE")
            if self.command=="GET" and length:return reject(400,"INVALID_REQUEST")
            body=self.rfile.read(length)
            if len(body)!=length:return reject(400,"INVALID_REQUEST")
            try:raw=body.decode("utf-8")
            except UnicodeDecodeError:return reject(400,"INVALID_JSON")
            status,payload=self.server.gateway.handle(self.command,self.path,self.headers.get("Authorization"),raw)
            self.reply(status,payload)
        except (OSError,TimeoutError):self.close_connection=True
        except Exception:
            # Runtime internal errors are never reflected to an untrusted client.
            try:reject(503,"DEPENDENCY_UNAVAILABLE")
            except OSError:self.close_connection=True
    do_GET=perform
    do_POST=perform
