import hashlib,pathlib
r=pathlib.Path('/app/src/cti_app');h=hashlib.sha256()
for p in sorted(r.rglob("*.py"),key=lambda q:str(q.relative_to(r))):
    h.update(str(p.relative_to(r)).encode());h.update(hashlib.sha256(p.read_bytes()).digest())
print(h.hexdigest()[:16])
