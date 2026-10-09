#!/usr/bin/env python3
"""Create the isolated desktop environment once, then open the local workspace."""
from pathlib import Path
import hashlib
import os
import subprocess
import sys
import venv

def main():
    if sys.version_info<(3,11):
        raise SystemExit('Python 3.11 or newer is required (tested with 3.13).')
    root=Path(__file__).resolve().parents[1]
    env=root/'.venv-feedback-workspace'
    if not env.exists():
        print('Creating isolated Python environment...',flush=True)
        venv.EnvBuilder(with_pip=True).create(env)
    python=env/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    files=[root/'requirements-feedback-learning.txt',root/'requirements-feedback-workspace.txt']
    fingerprint=hashlib.sha256(b'\0'.join(p.read_bytes() for p in files)).hexdigest()
    stamp=env/'workspace-dependencies.sha256'
    if not stamp.exists() or stamp.read_text().strip()!=fingerprint:
        print('Installing workspace dependencies. First launch requires Internet access.',flush=True)
        subprocess.run([str(python),'-m','pip','install','-r',str(files[1])],cwd=root,check=True)
        stamp.write_text(fingerprint+'\n')
    print('Opening local workspace. Keep this window open; Ctrl+C stops the server.',flush=True)
    return subprocess.call([str(python),str(root/'scripts/feedback_server.py')],cwd=root)
if __name__=='__main__':
    try:raise SystemExit(main())
    except subprocess.CalledProcessError as e:raise SystemExit(f'Environment installation failed: {e}')
