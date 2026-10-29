"""Load test for the deployed RingBreaker (stdlib + httpx, no extra tools).

  python deploy/loadtest.py https://ringbreaker.you.is-a.dev --user analyst --password ... --concurrency 20 --seconds 60

Measures throughput (req/s), latency percentiles and error rate for a realistic mix:
public demo page, and (with credentials) live API reads: overview, case list, case detail, account.
The investigator is excluded on purpose: it is rate-limited and bound by the LLM provider.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import time

import httpx


async def worker(client, base, paths, stop, lat, errs, codes):
    while time.perf_counter() < stop:
        path = random.choice(paths)
        t = time.perf_counter()
        try:
            r = await client.get(base + path)
            codes[r.status_code] = codes.get(r.status_code, 0) + 1
            if r.status_code >= 400:
                errs.append(path)
        except Exception:  # noqa: BLE001
            errs.append(path)
            codes["error"] = codes.get("error", 0) + 1
        lat.append((path.split("?")[0], (time.perf_counter() - t) * 1000))


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p / 100))] if xs else 0


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("--user")
    ap.add_argument("--password")
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--out", default="loadtest_results.json")
    a = ap.parse_args()
    base = a.base.rstrip("/")
    auth = (a.user, a.password) if a.user and a.password else None
    paths = ["/"]
    async with httpx.AsyncClient(auth=auth, timeout=30, http2=False, follow_redirects=True,
                                 limits=httpx.Limits(max_connections=a.concurrency)) as client:
        if auth:
            cases = (await client.get(base + "/live/api/cases")).json()
            case_ids = [c["case_id"] for c in cases[:10]]
            accts = [m for c in cases[:5] for m in c["member_ids"][:3]]
            paths = ["/", "/live/api/overview", "/live/api/cases"] + \
                [f"/live/api/cases/{c}" for c in case_ids] + [f"/live/api/accounts/{x}" for x in accts]
        lat, errs, codes = [], [], {}
        stop = time.perf_counter() + a.seconds
        t0 = time.perf_counter()
        await asyncio.gather(*[worker(client, base, paths, stop, lat, errs, codes) for _ in range(a.concurrency)])
        dur = time.perf_counter() - t0
    allms = [x for _, x in lat]
    by = {}
    for p, x in lat:
        key = "/live/api/cases/{id}" if p.startswith("/live/api/cases/") else "/live/api/accounts/{id}" if p.startswith("/live/api/accounts/") else p
        by.setdefault(key, []).append(x)
    res = {
        "target": base, "concurrency": a.concurrency, "seconds": round(dur, 1), "requests": len(lat),
        "throughput_rps": round(len(lat) / dur, 1), "error_rate": round(len(errs) / max(len(lat), 1), 4), "status_codes": codes,
        "latency_ms": {"p50": round(pct(allms, 50), 1), "p95": round(pct(allms, 95), 1), "p99": round(pct(allms, 99), 1),
                       "mean": round(statistics.mean(allms), 1) if allms else 0},
        "by_endpoint": {k: {"n": len(v), "p50": round(pct(v, 50), 1), "p95": round(pct(v, 95), 1)} for k, v in sorted(by.items())},
    }
    print(json.dumps(res, indent=2))
    with open(a.out, "w") as f:
        json.dump(res, f, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
