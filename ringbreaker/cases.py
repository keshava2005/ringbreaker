"""From scored accounts to investigable cases (mule rings).

Detection = union of three independent signals:
  1. model     - top-K accounts by mule-risk score (K = alert budget)
  2. cycle     - accounts in strongly connected components of large transfers
  3. expansion - graph expansion: an account that receives most of its money from
                 flagged accounts (the cash-out layer complaints never name), or
                 shares a device with two or more flagged accounts.
Connected components over money flow + shared devices become cases.
"""
from __future__ import annotations

import json
from collections import Counter

import networkx as nx
import numpy as np
import pandas as pd

EXCLUDE_SEGMENTS = {"merchant", "merchant_settlement", "corporate"}


def detect(con, scores: pd.DataFrame, budget: int = 250, min_score: float = 0.2) -> pd.DataFrame:
    s = scores.copy()
    people = s[~s.segment.isin(EXCLUDE_SEGMENTS)]
    ranked = people.sort_values("model_score", ascending=False)
    top = set(ranked[ranked.model_score >= min_score].account_id.head(budget))
    cyc = set(people[people.in_large_cycle == 1].account_id)
    flagged = top | cyc

    e = con.execute("SELECT src, dst, n, amount FROM gold.edges").df()
    ok = set(people.account_id)
    for _ in range(2):  # two hops of expansion
        into = e[e.dst.isin(ok) & ~e.dst.isin(flagged)]
        tot_in = into.groupby("dst").amount.sum()
        from_flag = into[into.src.isin(flagged)].groupby("dst").amount.sum()
        n_flag_senders = into[into.src.isin(flagged)].groupby("dst").src.nunique()
        share = (from_flag / tot_in).fillna(0)
        cand = share[(share >= 0.5) & (from_flag.reindex(share.index).fillna(0) >= 50000)].index
        cand = [c for c in cand if n_flag_senders.get(c, 0) >= 2 or share[c] >= 0.8]
        flagged |= set(cand)
    ad = con.execute("SELECT account_id, device_id FROM bronze.account_devices").df()
    dev_flag = ad[ad.account_id.isin(flagged)].groupby("device_id").account_id.nunique()
    hot_dev = set(dev_flag[dev_flag >= 2].index)
    dev_exp = set(ad[ad.device_id.isin(hot_dev)].account_id) & ok
    flagged |= dev_exp

    s["flag_model"] = s.account_id.isin(top)
    s["flag_cycle"] = s.account_id.isin(cyc)
    s["flag_expansion"] = s.account_id.isin(flagged) & ~s.flag_model & ~s.flag_cycle
    s["flagged"] = s.account_id.isin(flagged)
    return s


def build_cases(con, s: pd.DataFrame) -> tuple[list[dict], dict]:
    flagged = set(s.account_id[s.flagged])
    e = con.execute("SELECT src, dst, n, amount FROM gold.edges").df()
    ad = con.execute("SELECT account_id, device_id FROM bronze.account_devices").df()

    G = nx.Graph()
    G.add_nodes_from(sorted(flagged))  # sorted: deterministic regardless of PYTHONHASHSEED
    fe = e[e.src.isin(flagged) & e.dst.isin(flagged)].sort_values(["src", "dst"])
    for a, b, amt in zip(fe.src, fe.dst, fe.amount):
        w = G[a][b]["weight"] + amt if G.has_edge(a, b) else amt
        G.add_edge(a, b, weight=w)
    fd = ad[ad.account_id.isin(flagged)]
    for _, grp in fd.groupby("device_id"):
        acc = sorted(grp.account_id)
        for b in acc[1:]:
            G.add_edge(acc[0], b, weight=500_000)  # a shared phone is a strong tie
    comps = []
    for c in sorted(nx.connected_components(G), key=lambda c: (-len(c), min(c))):
        if len(c) < 2:
            continue
        if len(c) <= 8:
            comps.append(c)
            continue
        # large components can be two rings bridged by a false positive: split by modularity
        sub = nx.Graph()
        sub.add_nodes_from(sorted(c))
        sub.add_edges_from(sorted((min(a, b), max(a, b), d) for a, b, d in G.subgraph(c).edges(data=True)))
        parts = nx.community.louvain_communities(sub, weight="weight", resolution=0.05, seed=1)
        comps += [p for p in parts if len(p) >= 2]

    sidx = s.set_index("account_id")
    tx = con.execute("""
        SELECT t.txn_id, t.ts, t.src, t.dst, t.amount, t.channel, t.dst_mcc, t.is_cash, t.device_id
        FROM silver.transactions t
        WHERE t.src IN (SELECT UNNEST(?)) OR t.dst IN (SELECT UNNEST(?))
    """, [list(flagged), list(flagged)]).df()
    dwell = con.execute("""
        SELECT account_id, ts, amount, dwell_min FROM gold.dwell WHERE account_id IN (SELECT UNNEST(?))
    """, [list(flagged)]).df()
    complaints = con.execute("SELECT * FROM bronze.complaints").df()
    seg = con.execute("SELECT account_id, segment, bank, city, kyc, opened_date FROM silver.accounts").df().set_index("account_id")
    truth = con.execute("SELECT * FROM eval.ground_truth").df().set_index("account_id")

    cases = []
    for ci, comp in enumerate(sorted(comps, key=lambda c: (-len(c), min(c)))):
        mem = set(comp)
        t = tx[tx.src.isin(mem) | tx.dst.isin(mem)]
        internal = t[t.src.isin(mem) & t.dst.isin(mem) & (t.amount >= 2000)]
        ext_in = t[~t.src.isin(mem) & t.dst.isin(mem)]
        out_ext = t[t.src.isin(mem) & ~t.dst.isin(mem)]
        cash = out_ext[(out_ext.is_cash & (out_ext.amount >= 5000)) | (out_ext.dst_mcc == "Crypto OTC")]
        # suspected victim credits: external person-to-person credits >= 2k that were moved on within 24h
        d = dwell[dwell.account_id.isin(mem) & (dwell.dwell_min <= 1440) & (dwell.amount >= 2000)]
        vin = ext_in.merge(d[["account_id", "ts"]].rename(columns={"account_id": "dst"}), on=["dst", "ts"])
        vin = vin[~vin.src.map(seg.segment).isin(EXCLUDE_SEGMENTS)]
        if vin.empty and internal.empty:
            continue
        victims = vin.groupby("src").amount.sum().sort_values(ascending=False)
        start = (vin.ts.min() if len(vin) else internal.ts.min())
        end = max(cash.ts.max() if len(cash) else start, internal.ts.max() if len(internal) else start)

        members = []
        for a in mem:
            ti = t[t.dst == a]
            to = t[t.src == a]
            in_int = ti[ti.src.isin(mem)].amount.sum()
            in_ext = vin[vin.dst == a].amount.sum()
            out_cash = cash[cash.src == a].amount.sum()
            out_tot = to.amount.sum() or 1
            n_mem_senders = ti[ti.src.isin(mem)].src.nunique()
            if out_cash / out_tot > 0.45:
                role = "Cash-out"
            elif n_mem_senders >= 3 and in_int > in_ext:
                role = "Aggregator"
            elif in_int > in_ext:
                role = "Layer"
            else:
                role = "Collector"
            r = sidx.loc[a]
            members.append({
                "account_id": a, "role": role, "score": round(float(r.model_score), 4),
                "segment": r.segment, "bank": r.bank, "city": r.city, "kyc": r.kyc,
                "account_age_days": int(r.account_age_days), "flag": "cycle" if r.flag_cycle else (
                    "model" if r.flag_model else "expansion"),
                "victim_inflow": round(float(in_ext), 2), "cash_out": round(float(out_cash), 2),
                "reasons": json.loads(r.reasons), "complaints": int((complaints.reported_payee == a).sum()),
            })
        members.sort(key=lambda m: ({"Collector": 0, "Layer": 1, "Aggregator": 2, "Cash-out": 3}[m["role"]], -m["score"]))

        # typology guess from structure
        devs = ad[ad.account_id.isin(mem)].groupby("device_id").account_id.nunique()
        med_credit = float(vin.amount.median()) if len(vin) else 0
        if devs.max() >= 4 and med_credit < 15000:
            typ = "Device farm"
        elif sidx.loc[list(mem)].in_large_cycle.sum() >= 3:
            typ = "Circular layering"
        elif ((sidx.loc[list(mem)].new_device_in_window == 1) & (sidx.loc[list(mem)].account_age_days > 1000)
              & (sidx.loc[list(mem)].max_gap_days > 15)).sum() >= 2:
            typ = "Dormant takeover"
        else:
            typ = "Layered fan-out"

        v_amt = float(victims.sum())
        c_amt = float(cash.amount.sum())
        risk = 100 * (0.45 * max(m["score"] for m in members) + 0.25 * min(1, v_amt / 2_500_000)
                      + 0.15 * min(1, len(victims) / 40) + 0.15 * min(1, c_amt / max(v_amt, 1)))
        if typ == "Circular layering":
            risk = max(risk, 72.0)
        sev = "Critical" if risk >= 92 else "High" if risk >= 75 else "Medium" if risk >= 50 else "Low"
        comp_n = int(complaints.reported_payee.isin(mem).sum())
        rings = Counter(truth.loc[list(mem)].ring_id.dropna())
        ring, hits = rings.most_common(1)[0] if rings else (None, 0)
        ring_size = int((truth.ring_id == ring).sum()) if ring else 0
        cases.append({
            "case_id": f"CASE-{2026_1000 + ci}", "typology": typ, "severity": sev, "risk": round(risk, 1),
            "status": "Open", "members": members, "n_members": len(members), "n_victims": int(len(victims)),
            "victim_inflow": round(v_amt, 2), "cash_out": round(c_amt, 2),
            "in_flight": round(max(v_amt - c_amt, 0), 2), "complaints": comp_n,
            "internal_volume": round(float(internal.amount.sum()), 2),
            "shared_devices": int((devs >= 2).sum()), "median_victim_credit": round(med_credit, 2),
            "unreported_members": sum(1 for m in members if m["complaints"] == 0),
            "first_seen": str(start), "last_seen": str(end),
            "banks": sorted({m["bank"] for m in members}), "cities": sorted({m["city"] for m in members}),
            "detected_by": dict(Counter(m["flag"] for m in members)),
            "eval": {"purity": round(hits / len(mem), 3), "ring": ring, "ring_coverage": round(hits / ring_size, 3) if ring_size else 0},
            "_victims": victims, "_tx": t, "_vin": vin, "_cash": cash, "_internal": internal,
        })
    cases.sort(key=lambda c: -c["risk"])
    rings_total = truth.ring_id.nunique()
    found = {c["eval"]["ring"] for c in cases if c["eval"]["purity"] >= 0.5}
    summary = {
        "n_cases": len(cases), "rings_total": int(rings_total), "rings_found": len(found - {None}),
        "mean_purity": round(float(np.mean([c["eval"]["purity"] for c in cases])) if cases else 0, 3),
        "flagged_accounts": int(s.flagged.sum()),
        "flagged_true_mules": int(truth.loc[list(flagged)].is_mule.sum()),
        "mule_recall": round(float(truth.loc[list(flagged)].is_mule.sum() / truth.is_mule.sum()), 3),
        "by_source": {k: int(v) for k, v in {
            "model": s.flag_model.sum(), "cycle": s.flag_cycle.sum(), "expansion": s.flag_expansion.sum()}.items()},
        "expansion_true_mules": int(truth.loc[list(s.account_id[s.flag_expansion])].is_mule.sum()),
    }
    return cases, summary


def case_graph(c: dict, max_victims: int = 28, max_events: int = 700) -> dict:
    """Nodes/edges/timed events for the money-flow visual."""
    mem = {m["account_id"]: m for m in c["members"]}
    victims = c["_victims"]
    top_v = list(victims.index[:max_victims])
    rest = set(victims.index[max_victims:])
    nodes = [{"id": a, "kind": "member", "role": m["role"], "score": m["score"], "bank": m["bank"],
              "value": m["victim_inflow"] + m["cash_out"] + 1} for a, m in mem.items()]
    nodes += [{"id": v, "kind": "victim", "value": float(victims[v])} for v in top_v]
    if rest:
        nodes.append({"id": "OTHER_VICTIMS", "kind": "victim", "label": f"+{len(rest)} more victims",
                      "value": float(victims[list(rest)].sum())})
    sinks = set()
    events = []

    def key_src(x):
        return "OTHER_VICTIMS" if x in rest else x

    for r in c["_vin"].itertuples():
        events.append((r.ts, key_src(r.src), r.dst, r.amount, "victim"))
    for r in c["_internal"].itertuples():
        events.append((r.ts, r.src, r.dst, r.amount, "layer"))
    for r in c["_cash"].itertuples():
        sink = "ATM" if r.is_cash else r.dst
        sinks.add(sink)
        events.append((r.ts, r.src, sink, r.amount, "cashout"))
    for snk in sinks:
        nodes.append({"id": snk, "kind": "sink", "label": "ATM cash" if snk == "ATM" else f"Crypto OTC {snk}",
                      "value": float(sum(e[3] for e in events if e[2] == snk))})
    events.sort(key=lambda e: e[0])
    if len(events) > max_events:  # keep the shape: always keep big ones, sample the rest
        ev = pd.DataFrame(events, columns=["ts", "s", "t", "a", "k"])
        big = ev.nlargest(max_events // 2, "a")
        restv = ev.drop(big.index).sample(max_events - len(big), random_state=1)
        events = list(pd.concat([big, restv]).sort_values("ts").itertuples(index=False, name=None))
    agg = {}
    for ts, s_, t_, a, k in events:
        key = (s_, t_)
        if key not in agg:
            agg[key] = {"source": s_, "target": t_, "amount": 0.0, "n": 0, "kind": k}
        agg[key]["amount"] += a
        agg[key]["n"] += 1
    return {
        "nodes": nodes, "links": list(agg.values()),
        "events": [{"ts": str(ts), "source": s_, "target": t_, "amount": round(float(a), 2), "kind": k}
                   for ts, s_, t_, a, k in events],
    }
