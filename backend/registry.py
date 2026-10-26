from __future__ import annotations
import os, hashlib, uuid
def file_id(p): return str(uuid.uuid5(uuid.NAMESPACE_URL, "file:"+p))
def sha(b: bytes): return hashlib.sha256(b).hexdigest()
def scan(root, limit=10000):
    from registry.kinds import is_document
    out=[]
    for dp,_,fns in os.walk(root):
        for fn in fns:
            if is_document(fn):
                fp=os.path.join(dp,fn)
                try: st=os.stat(fp)
                except OSError: continue
                out.append({"file_id":file_id(fp),"source_path":fp,"bytes":st.st_size,"mtime":st.st_mtime})
                if len(out)>=limit: return out
    return out
