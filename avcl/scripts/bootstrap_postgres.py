"""Fetch the official Windows PostgreSQL ZIP's server files via HTTP ranges.

No installer, service registration or global PATH changes. ZIP CRCs and local
SHA-256 hashes are recorded; the source URL is the official EDB download target.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path,PurePosixPath
import hashlib
import io
import json
import urllib.request
import zipfile

ROOT=Path(__file__).resolve().parents[1]
URL="https://get.enterprisedb.com/postgresql/postgresql-18.6-5-windows-x64-binaries.zip"
SIZE=384620317
BLOCK=1024*1024
CACHE=ROOT/".runtime"/"pg-chunks"
DEST=ROOT/".runtime"/"postgresql-18.6"

class RemoteZip(io.RawIOBase):
    def __init__(self): self.pos=0;CACHE.mkdir(parents=True,exist_ok=True)
    def seekable(self): return True
    def readable(self): return True
    def tell(self): return self.pos
    def seek(self,offset,whence=0):
        self.pos=offset if whence==0 else self.pos+offset if whence==1 else SIZE+offset
        return self.pos
    def fetch(self,index):
        start=index*BLOCK;end=min(SIZE,start+BLOCK)-1;path=CACHE/f"{index:05d}.bin"
        if path.exists() and path.stat().st_size==end-start+1: return path
        req=urllib.request.Request(URL,headers={"Range":f"bytes={start}-{end}"})
        with urllib.request.urlopen(req,timeout=120) as response:
            if response.status!=206 or response.headers.get("Content-Range")!=f"bytes {start}-{end}/{SIZE}":
                raise RuntimeError("unexpected HTTP range response")
            data=response.read()
        if len(data)!=end-start+1: raise RuntimeError("incomplete range")
        temporary=path.with_suffix(".part");temporary.write_bytes(data);temporary.replace(path)
        return path
    def read(self,size=-1):
        end=SIZE if size<0 else min(SIZE,self.pos+size)
        output=bytearray()
        while self.pos<end:
            index=self.pos//BLOCK;data=self.fetch(index).read_bytes();offset=self.pos%BLOCK
            count=min(end-self.pos,len(data)-offset);output.extend(data[offset:offset+count]);self.pos+=count
        return bytes(output)

def main():
    reader=RemoteZip()
    with zipfile.ZipFile(reader) as archive:
        selected=[i for i in archive.infolist() if not i.is_dir() and
                  (i.filename.startswith(("pgsql/bin/","pgsql/lib/","pgsql/share/")) or i.filename=="pgsql/LICENSE")]
        blocks=set()
        for item in selected:
            # Include local header/filename/extra and compressed content.
            start=item.header_offset;end=start+30+len(item.filename.encode("utf-8"))+65535+item.compress_size
            blocks.update(range(start//BLOCK,min(SIZE-1,end)//BLOCK+1))
        print(f"Selected {len(selected)} server files, {len(blocks)} MiB range blocks",flush=True)
        with ThreadPoolExecutor(max_workers=8) as pool:
            for index,_ in enumerate(pool.map(reader.fetch,sorted(blocks)),1):
                if index%16==0 or index==len(blocks): print(f"Fetched {index}/{len(blocks)} blocks",flush=True)
        manifest={"version":"18.6","source":URL,"archive_size":SIZE,"selection":"bin,lib,share,LICENSE","files":{}}
        for item in selected:
            parts=PurePosixPath(item.filename).parts
            if any(p in {"..",""} for p in parts) or PurePosixPath(item.filename).is_absolute(): raise RuntimeError("unsafe ZIP entry")
            target=DEST.joinpath(*parts)
            if not target.resolve().is_relative_to(DEST.resolve()): raise RuntimeError("unsafe extraction path")
            data=archive.read(item)  # zipfile verifies each CRC.
            target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
            manifest["files"][item.filename]=hashlib.sha256(data).hexdigest()
        (DEST/"toolchain-manifest.json").write_text(json.dumps(manifest,indent=2),encoding="utf-8")
        print(f"Extracted to {DEST}",flush=True)

if __name__=="__main__": main()
