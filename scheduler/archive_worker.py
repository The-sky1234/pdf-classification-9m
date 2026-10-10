from __future__ import annotations
import os, hashlib, uuid
from classifier.taxonomy_v3 import target_dir
def safe_name(fid, src):
    base="".join(c if c.isalnum() or c in ("-","_",".") else "_" for c in os.path.basename(src))[-80:]
    return f"{fid[:8]}_{base or 'doc.pdf'}"
def archive_copy(src, root, atype, l1=None, l2=None, reason=None):
    assert os.path.isfile(src), "源缺失"
    rel = target_dir(atype,None,l1,l2,reason)
    rsrc, rroot = os.path.realpath(src), os.path.realpath(root)
    assert os.path.commonpath([rsrc,rroot])!=rroot, "源在归档根内，防循环"
    d = os.path.join(root, rel)
    os.makedirs(d, exist_ok=True)
    rd = os.path.realpath(d)
    assert rd==rroot or rd.startswith(rroot+os.sep), "穿越"
    h=hashlib.sha256()
    with open(src,"rb") as f:
        data=f.read()
    h.update(data); src_sha=h.hexdigest(); n=len(data)
    with open(src,"rb") as f:
        h2=hashlib.sha256(f.read()).hexdigest()
    assert h2==src_sha, "源变化"
    final=os.path.join(d, safe_name(src_sha, src))
    if os.path.exists(final):
        with open(final,"rb") as f: fh=hashlib.sha256(f.read()).hexdigest()
        if fh==src_sha and os.path.getsize(final)==n:
            return {"final":final,"sha":fh,"bytes":n,"dedup":True}
    tmp=os.path.join(d, f".part-{uuid.uuid4().hex}")
    with open(tmp,"wb") as f: f.write(data)
    with open(tmp,"rb") as f: th=hashlib.sha256(f.read()).hexdigest()
    assert th==src_sha and os.path.getsize(tmp)==n, "校验失败"
    os.replace(tmp, final)
    return {"final":final,"sha":src_sha,"bytes":n,"dedup":False}
