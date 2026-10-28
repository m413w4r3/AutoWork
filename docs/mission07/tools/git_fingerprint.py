"""Fingerprint of backend/src/cti_app/*.py in git; compare with container_fingerprint.py output."""
import hashlib
import pathlib
import subprocess

files = [
    f
    for f in subprocess.check_output(["git", "ls-files", "backend/src/cti_app"], text=True).split()
    if f.endswith(".py")
]
prefix = "backend/src/cti_app/"
h = hashlib.sha256()
for f in sorted(files, key=lambda f: f[len(prefix) :]):
    h.update(f[len(prefix) :].encode())
    h.update(hashlib.sha256(pathlib.Path(f).read_bytes()).digest())
print(h.hexdigest()[:16])
