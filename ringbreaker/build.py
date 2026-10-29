"""End-to-end build: simulate -> pipeline -> model -> cases -> app data (JSON).

  python -m ringbreaker build           # everything (about 1-2 minutes)
  python -m ringbreaker build --fast    # smaller world for tests / CI
"""
from __future__ import annotations

import json
import os
import time

import numpy as np
import pandas as pd

from . import cases as C
from . import model as M
from .pipeline import run_pipeline
from .simulate import simulate, write

ALERT_BUDGET = 250
PROFILE_FEATURES = ["in_cnt", "in_amt", "uniq_in", "out_cnt", "out_amt", "uniq_out", "pass_through",
                    "fast_fwd_ratio", "median_dwell_min", "cash_out_ratio", "crypto_ratio", "max_in_1h",
                    "max_gap_days", "account_age_days", "max_device_share", "n_devices", "new_device_in_window",
                    "pagerank", "in_from_fast_fwd", "in_large_cycle"]


def _clean(o):
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items() if not str(k).startswith("_")}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if np.isnan(o) else round(float(o), 4)
    if isinstance(o, float):
        return None if np.isnan(o) else o
    if isinstance(o, (pd.Timestamp,)):
        return str(o)
    return o


def _dump(obj, path):
    with open(path, "w") as f:
        json.dump(_clean(obj), f, default=str, separators=(",", ":"))


def build(data_dir: str = "data", fast: bool = False, log=print) -> dict:
    t0 = time.time()
    app = os.path.join(data_dir, "app")
    os.makedirs(os.path.join(app, "cases"), exist_ok=True)
    os.makedirs(os.path.join(app, "accounts"), exist_ok=True)

    log("1/5 simulating payment ecosystem…")
    tables = simulate(n_people=4000, n_merchants=300) if fast else simulate()
    write(tables, os.path.join(data_dir, "raw"))
    log(f"     {len(tables['transactions']):,} transactions, {len(tables['accounts']):,} accounts, "
        f"{len(tables['complaints']):,} complaints")

    log("2/5 running medallion pipeline (DuckDB SQL + graph analytics)…")
    con = run_pipeline(data_dir)

    log("3/5 training mule-risk model (GroupKFold, weak labels)…")
    scored = M.train_and_score(con, data_dir)
    ev = M.evaluate(scored)
    log(f"     PR-AUC model {ev['model_score']['pr_auc']} vs rules {ev['rules_score']['pr_auc']}")

    log("4/5 detecting rings and building cases…")
    s = C.detect(con, scored, ALERT_BUDGET)
    cases, csum = C.build_cases(con, s)
    log(f"     {csum['n_cases']} cases, {csum['rings_found']}/{csum['rings_total']} planted rings found")

    log("5/5 writing app data…")
    keep = s[["account_id", "model_score", "rules_score", "reasons", "flagged", "flag_model", "flag_cycle",
              "flag_expansion", "reported", "n_complaints", "segment", "bank", "city", "kyc", "age"] + PROFILE_FEATURES]
    con.register("scores_df", keep)
    con.execute("CREATE OR REPLACE TABLE gold.scores AS SELECT * FROM scores_df")
    con.unregister("scores_df")

    # ---- per-case files -------------------------------------------------------------
    member_case = {}
    case_rows = []
    for c in cases:
        g = C.case_graph(c)
        vin, cash = c["_vin"], c["_cash"]
        days = pd.date_range("2026-08-01", "2026-09-30")
        v_d = vin.groupby(vin.ts.dt.normalize()).amount.sum().reindex(days, fill_value=0)
        c_d = cash.groupby(cash.ts.dt.normalize()).amount.sum().reindex(days, fill_value=0)
        detail = dict(c)
        detail["graph"] = g
        detail["daily"] = {"dates": [str(d.date()) for d in days], "victim_inflow": v_d.round(0).tolist(),
                           "cash_out": c_d.round(0).tolist()}
        detail["top_victim_credits"] = vin.nlargest(12, "amount")[["ts", "src", "dst", "amount", "channel"]].to_dict("records")
        _dump(detail, os.path.join(app, "cases", f"{c['case_id']}.json"))
        for m in c["members"]:
            member_case[m["account_id"]] = c["case_id"]
        case_rows.append({k: v for k, v in c.items() if k not in ("members",) and not k.startswith("_")}
                         | {"member_ids": [m["account_id"] for m in c["members"]],
                            "roles": dict(pd.Series([m["role"] for m in c["members"]]).value_counts())})
    _dump(case_rows, os.path.join(app, "cases.json"))

    # ---- whole-network map (members + money links between them) ----------------------
    net_nodes, net_links = [], []
    for c in cases:
        for m in c["members"]:
            net_nodes.append({"id": m["account_id"], "case_id": c["case_id"], "role": m["role"], "score": m["score"],
                              "severity": c["severity"], "value": m["victim_inflow"] + m["cash_out"] + 1})
        it = c["_internal"].groupby(["src", "dst"]).amount.sum().reset_index()
        net_links += [{"source": r.src, "target": r.dst, "amount": float(r.amount), "case_id": c["case_id"]}
                      for r in it.itertuples()]
    _dump({"nodes": net_nodes, "links": net_links}, os.path.join(app, "network.json"))

    # ---- account 360 files (flagged + case members + top singletons) -------------------
    ids = set(s.account_id[s.flagged]) | set(member_case)
    tx = con.execute("""
        SELECT ts, src, dst, amount, channel, txn_type, dst_mcc FROM silver.transactions
        WHERE src IN (SELECT UNNEST(?)) OR dst IN (SELECT UNNEST(?))""", [list(ids), list(ids)]).df()
    days = pd.date_range("2026-08-01", "2026-09-30")
    sidx = keep.set_index("account_id")
    acc_meta = con.execute("SELECT account_id, opened_date, vpa FROM silver.accounts").df().set_index("account_id")
    devs = con.execute("""SELECT ad.account_id, d.device_id, d.os, d.first_seen,
                          (SELECT COUNT(*) FROM bronze.account_devices x WHERE x.device_id = d.device_id) AS shared_with
                          FROM bronze.account_devices ad JOIN bronze.devices d USING (device_id)
                          WHERE ad.account_id IN (SELECT UNNEST(?))""", [list(ids)]).df()
    index = []
    for a in ids:
        r = sidx.loc[a]
        ti, to = tx[tx.dst == a], tx[tx.src == a]
        cp_in = ti.groupby("src").amount.agg(["sum", "count"]).nlargest(8, "sum").reset_index()
        cp_out = to.groupby("dst").amount.agg(["sum", "count"]).nlargest(8, "sum").reset_index()
        vpa = acc_meta.loc[a, "vpa"]
        prof = {
            "account_id": a, "case_id": member_case.get(a), "score": float(r.model_score), "rules_score": float(r.rules_score),
            "reasons": json.loads(r.reasons), "segment": r.segment, "bank": r.bank, "city": r.city, "kyc": r.kyc,
            "age": None if pd.isna(r.age) else int(r.age), "opened_date": str(acc_meta.loc[a, "opened_date"].date()),
            "vpa_masked": vpa[:2] + "•••" + vpa[vpa.index("@") - 2:],
            "reported": int(r.reported), "complaints": int(r.n_complaints),
            "flags": [f for f, on in (("model", r.flag_model), ("cycle", r.flag_cycle), ("expansion", r.flag_expansion)) if on],
            "features": {f: (None if pd.isna(r[f]) else round(float(r[f]), 4)) for f in PROFILE_FEATURES},
            "devices": devs[devs.account_id == a].drop(columns="account_id").to_dict("records"),
            "top_senders": cp_in.rename(columns={"src": "account_id"}).to_dict("records"),
            "top_recipients": cp_out.rename(columns={"dst": "account_id"}).to_dict("records"),
            "daily": {"in": ti.groupby(ti.ts.dt.normalize()).amount.sum().reindex(days, fill_value=0).round(0).tolist(),
                      "out": to.groupby(to.ts.dt.normalize()).amount.sum().reindex(days, fill_value=0).round(0).tolist()},
            "recent": pd.concat([ti.assign(dir="in"), to.assign(dir="out")]).sort_values("ts", ascending=False)
                        .head(25)[["ts", "dir", "src", "dst", "amount", "channel", "txn_type"]].to_dict("records"),
        }
        _dump(prof, os.path.join(app, "accounts", f"{a}.json"))
        index.append({"account_id": a, "score": float(r.model_score), "case_id": member_case.get(a),
                      "segment": r.segment, "bank": r.bank, "reported": int(r.reported),
                      "flags": prof["flags"], "in_amt": float(r.in_amt)})
    index.sort(key=lambda x: -x["score"])
    _dump(index, os.path.join(app, "alerts.json"))

    # ---- model + pipeline ---------------------------------------------------------------
    ev["cases"] = csum
    ev["alert_budget"] = ALERT_BUDGET
    score_hist = np.histogram(s.model_score, bins=40, range=(0, 1))
    mule_hist = np.histogram(s.model_score[s.account_id.isin(set(con.execute(
        "SELECT account_id FROM eval.ground_truth WHERE is_mule=1").df().account_id))], bins=40, range=(0, 1))
    ev["score_hist"] = {"bins": score_hist[1][:-1].round(3).tolist(), "all": score_hist[0].tolist(), "mules": mule_hist[0].tolist()}
    _dump(ev, os.path.join(app, "model.json"))
    with open(os.path.join(data_dir, "pipeline_run.json")) as f:
        pipe = json.load(f)
    pipe["stages"].append({"name": "Score & detect", "kind": "serve", "inputs": ["gold.features"],
                           "outputs": ["gold.scores", "cases"], "rows": {"accounts scored": int(len(s)),
                           "model alerts (score ≥ 0.2)": int(s.flag_model.sum()), "flagged after expansion": int(s.flagged.sum()),
                           "cases": csum["n_cases"]}, "checks": [], "status": "ok", "duration_ms": None})
    _dump(pipe, os.path.join(app, "pipeline.json"))

    # ---- overview -------------------------------------------------------------------------
    daily = con.execute("""
        SELECT CAST(ts AS DATE) d, COUNT(*) n, SUM(amount) amt FROM silver.transactions GROUP BY 1 ORDER BY 1""").df()
    vin_all = pd.concat([c["_vin"] for c in cases]) if cases else pd.DataFrame(columns=["ts", "amount"])
    cash_all = pd.concat([c["_cash"] for c in cases]) if cases else pd.DataFrame(columns=["ts", "amount"])
    comp = con.execute("SELECT CAST(reported_at AS DATE) d, COUNT(*) n FROM bronze.complaints GROUP BY 1").df()
    dts = pd.to_datetime(daily.d)
    overview = {
        "window": ["2026-08-01", "2026-09-30"],
        "kpis": {
            "transactions": int(daily.n.sum()), "volume": float(daily.amt.sum()),
            "accounts": int(len(s)), "cases": len(cases),
            "critical_cases": sum(1 for c in cases if c["severity"] == "Critical"),
            "victim_inflow": float(sum(c["victim_inflow"] for c in cases)),
            "cash_out": float(sum(c["cash_out"] for c in cases)),
            "in_flight": float(sum(c["in_flight"] for c in cases)),
            "victims": int(sum(c["n_victims"] for c in cases)),
            "flagged": int(s.flagged.sum()), "unreported_flagged": int((s.flagged & (s.reported == 0)).sum()),
            "complaints": int(comp.n.sum()),
            "precision_at_budget": next(b for b in ev["model_score"]["at_k"] if b["k"] >= ALERT_BUDGET)["precision"],
            "rings_found": csum["rings_found"], "rings_total": csum["rings_total"],
        },
        "daily": {
            "dates": [str(d.date()) for d in dts], "txns": daily.n.tolist(), "volume": daily.amt.round(0).tolist(),
            "victim_inflow": vin_all.groupby(vin_all.ts.dt.normalize()).amount.sum().reindex(dts, fill_value=0).round(0).tolist(),
            "cash_out": cash_all.groupby(cash_all.ts.dt.normalize()).amount.sum().reindex(dts, fill_value=0).round(0).tolist(),
            "complaints": comp.set_index(pd.to_datetime(comp.d)).n.reindex(dts, fill_value=0).tolist(),
        },
        "typology": [{"typology": t, "cases": int(g.case_id.count()), "victim_inflow": float(g.victim_inflow.sum()),
                      "members": int(g.n_members.sum())}
                     for t, g in pd.DataFrame(case_rows).groupby("typology")] if case_rows else [],
        "banks": [{"bank": b, "flagged": int(n)} for b, n in s[s.flagged].bank.value_counts().items()],
        "severity": {k: sum(1 for c in cases if c["severity"] == k) for k in ("Critical", "High", "Medium", "Low")},
        "sources": csum["by_source"],
    }
    _dump(overview, os.path.join(app, "overview.json"))
    con.close()
    log(f"done in {time.time() - t0:.0f}s -> {app}/")
    return overview
