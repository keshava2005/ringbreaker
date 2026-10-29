"""Build a self-contained demo snapshot (one HTML file, data embedded) for sharing.

  python -m ringbreaker export-demo      # -> dist/ringbreaker-demo.html
"""
from __future__ import annotations

import glob
import json
import os
import re

from .investigator import investigate_rules
from .store import Store

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "index.html")


def snapshot(data_dir: str = "data") -> dict:
    st = Store(data_dir)
    app = os.path.join(data_dir, "app")
    case_ids = [c["case_id"] for c in st.cases()]
    details = {cid: {k: v for k, v in st.case(cid).items() if k not in ("history", "investigation", "status")} for cid in case_ids}
    accounts = {}
    for p in glob.glob(os.path.join(app, "accounts", "*.json")):
        with open(p) as f:
            a = json.load(f)
        accounts[a["account_id"]] = a
    inv = {}
    for cid in case_ids:
        steps, final = [], None
        for ev in investigate_rules(st, cid):
            if ev["type"] == "step":
                steps.append(ev)
            elif ev["type"] == "final":
                final = ev
        inv[cid] = {"report": final["report"], "engine": final["engine"], "steps": steps}
    return {"overview": st.overview(), "cases": st.cases(), "caseDetails": details, "accounts": accounts,
            "alerts": st.alerts(), "model": st.model(), "pipeline": st.pipeline(), "network": st.network(),
            "investigations": inv}


def export_demo(data_dir: str = "data", out: str = "dist/ringbreaker-demo.html", fragment: bool = False) -> str:
    data = snapshot(data_dir)
    blob = json.dumps(data, default=str, separators=(",", ":")).replace("</", "<\\/")
    with open(WEB, encoding="utf-8") as f:
        html = f.read()
    html = html.replace("/*__DATA__*/", blob)
    if fragment:  # artifact hosts wrap the page in their own document skeleton
        head = re.search(r"<head>(.*?)</head>", html, re.S).group(1)
        head = re.sub(r'<meta charset="utf-8">\s*|<meta name="viewport"[^>]*>\s*', "", head)
        body = re.search(r"<body>(.*)</body>", html, re.S).group(1)
        html = head.strip() + "\n" + body.strip() + "\n"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    return out


def export_site(data_dir: str = "data", out_dir: str = "dist/site") -> str:
    """Public static site: the demo snapshot as index.html plus the architecture page."""
    os.makedirs(out_dir, exist_ok=True)
    export_demo(data_dir, os.path.join(out_dir, "index.html"))
    arch = os.path.join(os.path.dirname(WEB), "..", "docs", "architecture.html")
    if os.path.exists(arch):
        with open(arch, encoding="utf-8") as f:
            frag = f.read()
        page = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
                '</head><body>' + frag + "</body></html>")
        with open(os.path.join(out_dir, "architecture.html"), "w", encoding="utf-8") as f:
            f.write(page)
    return out_dir
