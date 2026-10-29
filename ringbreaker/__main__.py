"""RingBreaker command line.

  python -m ringbreaker build                 simulate data, run pipeline, train model, build cases
  python -m ringbreaker serve                 dashboard + API on http://127.0.0.1:8000 (docs at /docs)
  python -m ringbreaker investigate CASE-ID   run the investigator on one case and print the report
  python -m ringbreaker export-demo           one-file demo snapshot -> dist/ringbreaker-demo.html
"""
from __future__ import annotations

import argparse
import json
import os
import sys


def _load_env(path=".env"):
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main(argv=None):
    _load_env()
    p = argparse.ArgumentParser(prog="ringbreaker", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", default=os.environ.get("RINGBREAKER_DATA_DIR", "data"))
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--fast", action="store_true", help="smaller world (CI / quick test)")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--host", default="127.0.0.1", help="0.0.0.0 only inside a container behind a proxy")
    i = sub.add_parser("investigate")
    i.add_argument("case_id")
    e = sub.add_parser("export-demo")
    e.add_argument("--out", default="dist/ringbreaker-demo.html")
    e.add_argument("--fragment", action="store_true", help=argparse.SUPPRESS)
    e.add_argument("--site", metavar="DIR", help="write the public static site (index + architecture) to DIR")
    pk = sub.add_parser("package", help="zip the source for AWS deployment -> deploy/.build/ringbreaker-app.zip")
    pk.add_argument("--out", default="deploy/.build/ringbreaker-app.zip")
    a = p.parse_args(argv)

    if a.cmd == "build":
        from .build import build
        build(a.data, fast=a.fast)
    elif a.cmd == "serve":
        if not os.path.exists(os.path.join(a.data, "app", "overview.json")):
            sys.exit("No data yet. Run: python -m ringbreaker build")
        from .api import serve
        serve(a.port, a.data, host=a.host)
    elif a.cmd == "investigate":
        from .investigator import investigate
        from .store import Store
        st = Store(a.data)
        for ev in investigate(st, a.case_id.upper(), cache_dir=os.path.join(a.data, ".llm_cache")):
            if ev["type"] == "step":
                print(f"[{ev['n']}] {ev['tool']}({json.dumps(ev['args'])})  {ev.get('why', '')}")
            elif ev["type"] == "final":
                print(json.dumps(ev["report"], indent=2, ensure_ascii=False))
                print(f"\nengine: {ev['engine']}  tokens: {ev['tokens']}")
            else:
                print(ev)
    elif a.cmd == "package":
        import zipfile
        skip_dirs = {"data", ".venv", "venv", "dist", ".git", "__pycache__", ".pytest_cache", ".terraform", ".build", "state"}
        skip_files = {".env", "terraform.tfstate", "terraform.tfstate.backup", ".terraform.lock.hcl"}
        os.makedirs(os.path.dirname(a.out), exist_ok=True)
        n = 0
        with zipfile.ZipFile(a.out, "w", zipfile.ZIP_DEFLATED) as z:
            for root, dirs, files in os.walk("."):
                dirs[:] = sorted(d for d in dirs if d not in skip_dirs)
                for f in sorted(files):
                    if f in skip_files or f.endswith((".tfvars", ".pyc", ".zip")):
                        continue
                    p = os.path.join(root, f)
                    z.write(p, os.path.relpath(p, "."))
                    n += 1
        print(f"wrote {a.out} ({n} files, secrets and data excluded)")
    elif a.cmd == "export-demo":
        from .export import export_demo, export_site
        if a.site:
            print("wrote site to", export_site(a.data, a.site))
        else:
            print("wrote", export_demo(a.data, a.out, fragment=a.fragment))


if __name__ == "__main__":
    main()
