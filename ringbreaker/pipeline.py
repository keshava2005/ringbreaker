"""Medallion pipeline: bronze (raw parquet) -> silver (validated) -> gold (features).

Every stage records rows in/out, duration and data-quality checks into
data/pipeline_run.json, which the dashboard renders as a DAG.
Feature engineering is plain SQL (DuckDB), so it ports to BigQuery/Athena/Snowflake.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import duckdb
import networkx as nx
import numpy as np
import pandas as pd

RAW = "raw"
WINDOW_START = "2026-08-01"
WINDOW_END = "2026-10-01"


class Run:
    def __init__(self):
        self.stages: list[dict] = []
        self.started = datetime.now(timezone.utc)

    def stage(self, name, kind, inputs, outputs):
        run = self

        class _S:
            def __enter__(s):
                s.t = time.perf_counter()
                s.rec = {"name": name, "kind": kind, "inputs": inputs, "outputs": outputs, "rows": {}, "checks": []}
                return s.rec

            def __exit__(s, *exc):
                s.rec["duration_ms"] = round((time.perf_counter() - s.t) * 1000)
                s.rec["status"] = "failed" if exc[0] else (
                    "warn" if any(c["status"] == "warn" for c in s.rec["checks"]) else "ok")
                run.stages.append(s.rec)
                return False
        return _S()

    def save(self, path):
        with open(path, "w") as f:
            json.dump({"started_at": self.started.isoformat(), "finished_at": datetime.now(timezone.utc).isoformat(),
                       "stages": self.stages}, f, indent=2, default=str)


def _check(rec, con, name, sql, expect="zero", warn_only=False):
    v = con.execute(sql).fetchone()[0]
    ok = (v == 0) if expect == "zero" else bool(v)
    rec["checks"].append({"name": name, "value": v if not isinstance(v, (np.generic,)) else v.item(),
                          "status": "pass" if ok else ("warn" if warn_only else "fail")})
    if not ok and not warn_only:
        raise ValueError(f"data quality check failed: {name} ({v})")


def run_pipeline(data_dir: str = "data") -> duckdb.DuckDBPyConnection:
    raw = os.path.join(data_dir, RAW)
    db_path = os.path.join(data_dir, "warehouse.duckdb")
    if os.path.exists(db_path):
        os.remove(db_path)
    con = duckdb.connect(db_path)
    run = Run()

    # ---------------- bronze ------------------------------------------------------
    with run.stage("Ingest", "bronze", ["raw/*.parquet"], ["bronze.*"]) as rec:
        con.execute("CREATE SCHEMA bronze; CREATE SCHEMA silver; CREATE SCHEMA gold; CREATE SCHEMA eval;")
        for t in ["accounts", "devices", "account_devices", "transactions", "complaints"]:
            p = os.path.join(raw, f"{t}.parquet").replace("'", "''")
            con.execute(f"CREATE TABLE bronze.{t} AS SELECT * FROM read_parquet('{p}')")
            rec["rows"][t] = con.execute(f"SELECT COUNT(*) FROM bronze.{t}").fetchone()[0]
        # ground truth is kept apart: only evaluation may read it
        p = os.path.join(raw, "ground_truth.parquet").replace("'", "''")
        con.execute(f"CREATE TABLE eval.ground_truth AS SELECT * FROM read_parquet('{p}')")

    # ---------------- silver ------------------------------------------------------
    with run.stage("Validate & clean", "silver", ["bronze.*"], ["silver.transactions", "silver.accounts"]) as rec:
        _check(rec, con, "transactions: duplicate txn_id",
               "SELECT COUNT(*) - COUNT(DISTINCT txn_id) FROM bronze.transactions")
        _check(rec, con, "transactions: null src/dst/amount",
               "SELECT COUNT(*) FROM bronze.transactions WHERE src IS NULL OR dst IS NULL OR amount IS NULL")
        _check(rec, con, "transactions: non-positive amount", "SELECT COUNT(*) FROM bronze.transactions WHERE amount <= 0")
        _check(rec, con, "transactions: timestamp inside window",
               f"SELECT COUNT(*) FROM bronze.transactions WHERE ts < TIMESTAMP '{WINDOW_START}' OR ts >= TIMESTAMP '{WINDOW_END}'")
        _check(rec, con, "transactions: orphan account references",
               "SELECT COUNT(*) FROM bronze.transactions t WHERE t.src NOT IN (SELECT account_id FROM bronze.accounts) "
               "OR (t.dst <> 'ATM' AND t.dst NOT IN (SELECT account_id FROM bronze.accounts))")
        _check(rec, con, "complaints: payee exists", "SELECT COUNT(*) FROM bronze.complaints WHERE reported_payee NOT IN "
               "(SELECT account_id FROM bronze.accounts)")
        _check(rec, con, "accounts: duplicate account_id", "SELECT COUNT(*) - COUNT(DISTINCT account_id) FROM bronze.accounts")
        _check(rec, con, "transactions: failed share under 3%",
               "SELECT (AVG(CASE WHEN status='FAILED' THEN 1.0 ELSE 0 END) > 0.03)::INT FROM bronze.transactions", warn_only=True)
        con.execute("""
            CREATE TABLE silver.transactions AS
            SELECT t.*, (t.dst = 'ATM') AS is_cash,
                   coalesce(m.mcc, '') AS dst_mcc
            FROM bronze.transactions t
            LEFT JOIN bronze.accounts m ON m.account_id = t.dst
            WHERE t.status = 'SUCCESS'""")
        con.execute("CREATE TABLE silver.accounts AS SELECT * FROM bronze.accounts")
        rec["rows"]["silver.transactions"] = con.execute("SELECT COUNT(*) FROM silver.transactions").fetchone()[0]

    # ---------------- gold: behavioural features (SQL) -----------------------------
    with run.stage("Behavioural features", "gold", ["silver.transactions"], ["gold.flow_features"]) as rec:
        con.execute("""
        CREATE TABLE gold.inflow AS
            SELECT dst AS account_id, src AS cpty, ts, amount FROM silver.transactions WHERE dst <> 'ATM';
        CREATE TABLE gold.outflow AS
            SELECT src AS account_id, dst AS cpty, ts, amount, is_cash, dst_mcc FROM silver.transactions;

        -- time from each inbound credit to the next outbound debit (ASOF join)
        CREATE TABLE gold.dwell AS
            SELECT i.account_id, i.ts, i.amount,
                   date_diff('minute', i.ts, o.ts) AS dwell_min
            FROM gold.inflow i
            ASOF LEFT JOIN gold.outflow o
              ON i.account_id = o.account_id AND o.ts >= i.ts;

        -- busiest 1-hour window of inbound credits (window function over a time range)
        CREATE TABLE gold.burst AS
            SELECT account_id, MAX(n_1h) AS max_in_1h FROM (
              SELECT account_id, COUNT(*) OVER (PARTITION BY account_id ORDER BY ts
                     RANGE BETWEEN INTERVAL 1 HOUR PRECEDING AND CURRENT ROW) AS n_1h
              FROM gold.inflow) GROUP BY 1;

        -- longest silence between two transactions (dormant then suddenly active)
        CREATE TABLE gold.gaps AS
            SELECT account_id, MAX(gap_d) AS max_gap_days, MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM (
              SELECT account_id, ts, date_diff('hour', lag(ts, 1, TIMESTAMP '2026-08-01') OVER (PARTITION BY account_id ORDER BY ts), ts) / 24.0 AS gap_d
              FROM (SELECT account_id, ts FROM gold.inflow UNION ALL SELECT account_id, ts FROM gold.outflow)
            ) GROUP BY 1;

        CREATE TABLE gold.flow_features AS
        WITH i AS (
            SELECT account_id, COUNT(*) in_cnt, SUM(amount) in_amt, COUNT(DISTINCT cpty) uniq_in,
                   MAX(amount) max_in, COUNT(DISTINCT CAST(ts AS DATE)) in_days,
                   AVG(CASE WHEN hour(ts) BETWEEN 0 AND 5 THEN 1.0 ELSE 0 END) night_in_ratio
            FROM gold.inflow GROUP BY 1),
        o AS (
            SELECT account_id, COUNT(*) out_cnt, SUM(amount) out_amt, COUNT(DISTINCT cpty) uniq_out,
                   SUM(CASE WHEN is_cash THEN amount ELSE 0 END) cash_amt,
                   SUM(CASE WHEN dst_mcc = 'Crypto OTC' THEN amount ELSE 0 END) crypto_amt,
                   SUM(CASE WHEN dst_mcc <> '' THEN amount ELSE 0 END) merchant_amt
            FROM gold.outflow GROUP BY 1),
        d AS (
            SELECT account_id,
                   median(dwell_min) AS median_dwell_min,
                   SUM(CASE WHEN dwell_min <= 60 THEN amount ELSE 0 END) / NULLIF(SUM(amount), 0) AS fast_fwd_ratio
            FROM gold.dwell GROUP BY 1)
        SELECT a.account_id,
               coalesce(i.in_cnt, 0) in_cnt, coalesce(i.in_amt, 0) in_amt, coalesce(i.uniq_in, 0) uniq_in,
               coalesce(i.max_in, 0) max_in, coalesce(i.in_days, 0) in_days, coalesce(i.night_in_ratio, 0) night_in_ratio,
               coalesce(o.out_cnt, 0) out_cnt, coalesce(o.out_amt, 0) out_amt, coalesce(o.uniq_out, 0) uniq_out,
               coalesce(o.out_amt / NULLIF(i.in_amt, 0), 0) AS pass_through,
               coalesce(o.cash_amt / NULLIF(o.out_amt, 0), 0) AS cash_out_ratio,
               coalesce(o.crypto_amt / NULLIF(o.out_amt, 0), 0) AS crypto_ratio,
               coalesce(o.merchant_amt / NULLIF(o.out_amt, 0), 0) AS merchant_spend_ratio,
               coalesce(d.median_dwell_min, 99999) median_dwell_min, coalesce(d.fast_fwd_ratio, 0) fast_fwd_ratio,
               coalesce(b.max_in_1h, 0) max_in_1h, coalesce(g.max_gap_days, 61) max_gap_days,
               g.first_ts, g.last_ts
        FROM silver.accounts a
        LEFT JOIN i USING (account_id) LEFT JOIN o USING (account_id) LEFT JOIN d USING (account_id)
        LEFT JOIN gold.burst b USING (account_id) LEFT JOIN gold.gaps g USING (account_id);
        """)
        rec["rows"]["gold.flow_features"] = con.execute("SELECT COUNT(*) FROM gold.flow_features").fetchone()[0]
        _check(rec, con, "features: one row per account",
               "SELECT (SELECT COUNT(*) FROM gold.flow_features) - (SELECT COUNT(*) FROM silver.accounts)")
        _check(rec, con, "features: pass_through not negative", "SELECT COUNT(*) FROM gold.flow_features WHERE pass_through < 0")

    with run.stage("Identity features", "gold", ["silver.accounts", "bronze.devices", "bronze.account_devices"],
                   ["gold.identity_features"]) as rec:
        con.execute("""
        CREATE TABLE gold.identity_features AS
        WITH dev_share AS (
            SELECT device_id, COUNT(DISTINCT account_id) n_acc FROM bronze.account_devices GROUP BY 1),
        ip_share AS (
            SELECT d.ip_prefix, COUNT(DISTINCT ad.account_id) n_acc
            FROM bronze.account_devices ad JOIN bronze.devices d USING (device_id) GROUP BY 1),
        per AS (
            SELECT ad.account_id, MAX(ds.n_acc) max_device_share, MAX(ip.n_acc) max_ip_share,
                   COUNT(DISTINCT ad.device_id) n_devices,
                   MAX(CASE WHEN d.first_seen >= TIMESTAMP '2026-08-01' THEN 1 ELSE 0 END) new_device_in_window
            FROM bronze.account_devices ad
            JOIN dev_share ds USING (device_id) JOIN bronze.devices d USING (device_id)
            JOIN ip_share ip ON ip.ip_prefix = d.ip_prefix
            GROUP BY 1)
        SELECT a.account_id, a.segment, a.bank, a.city, a.kyc, a.age,
               date_diff('day', a.opened_date, TIMESTAMP '2026-10-01') AS account_age_days,
               (a.kyc = 'MIN')::INT AS kyc_min,
               coalesce(p.max_device_share, 1) max_device_share, coalesce(p.max_ip_share, 1) max_ip_share,
               coalesce(p.n_devices, 1) n_devices, coalesce(p.new_device_in_window, 0) new_device_in_window
        FROM silver.accounts a LEFT JOIN per p USING (account_id);
        """)
        rec["rows"]["gold.identity_features"] = con.execute("SELECT COUNT(*) FROM gold.identity_features").fetchone()[0]

    # ---------------- graph features ------------------------------------------------
    with run.stage("Graph analytics", "gold", ["silver.transactions", "gold.flow_features"], ["gold.graph_features", "gold.edges"]) as rec:
        con.execute("""
        CREATE TABLE gold.edges AS
            SELECT src, dst, COUNT(*) n, SUM(amount) amount, MIN(ts) first_ts, MAX(ts) last_ts
            FROM silver.transactions WHERE dst <> 'ATM' GROUP BY 1, 2""")
        e = con.execute("SELECT src, dst, amount FROM gold.edges").df()
        G = nx.DiGraph()
        G.add_weighted_edges_from(e.itertuples(index=False, name=None))
        pr = nx.pagerank(G, alpha=0.85, weight="weight", max_iter=60, tol=1e-6)
        # neighbour behaviour: does money arrive from / go to accounts that forward fast?
        ff = con.execute("SELECT account_id, fast_fwd_ratio, pass_through FROM gold.flow_features").df().set_index("account_id")
        fast = set(ff.index[(ff.fast_fwd_ratio > 0.5) & (ff.pass_through > 0.7)])
        e["src_fast"] = e.src.isin(fast)
        e["dst_fast"] = e.dst.isin(fast)
        in_from_fast = e.assign(a=e.amount * e.src_fast).groupby("dst").a.sum() / e.groupby("dst").amount.sum()
        out_to_fast = e.assign(a=e.amount * e.dst_fast).groupby("src").a.sum() / e.groupby("src").amount.sum()
        # reciprocity / short cycles among large transfers (round-tripping signal)
        big = e[e.amount >= 50000]
        H = nx.DiGraph()
        H.add_edges_from(zip(big.src, big.dst))
        in_cycle = set()
        for comp in nx.strongly_connected_components(H):
            if 3 <= len(comp) <= 12:
                in_cycle |= comp
        gf = pd.DataFrame({"account_id": ff.index})
        gf["pagerank"] = gf.account_id.map(pr).fillna(0) * 1e4
        gf["in_degree"] = gf.account_id.map(dict(G.in_degree())).fillna(0)
        gf["out_degree"] = gf.account_id.map(dict(G.out_degree())).fillna(0)
        gf["in_from_fast_fwd"] = gf.account_id.map(in_from_fast).fillna(0)
        gf["out_to_fast_fwd"] = gf.account_id.map(out_to_fast).fillna(0)
        gf["in_large_cycle"] = gf.account_id.isin(in_cycle).astype(int)
        con.register("gf", gf)
        con.execute("CREATE TABLE gold.graph_features AS SELECT * FROM gf")
        rec["rows"]["graph nodes"] = G.number_of_nodes()
        rec["rows"]["graph edges"] = G.number_of_edges()
        rec["rows"]["accounts in large cycles"] = len(in_cycle)

    with run.stage("Feature store", "gold", ["gold.flow_features", "gold.identity_features", "gold.graph_features"],
                   ["gold.features"]) as rec:
        con.execute("""
        CREATE TABLE gold.features AS
        SELECT f.*, idf.* EXCLUDE (account_id), g.* EXCLUDE (account_id),
               f.in_amt / NULLIF(median(f.in_amt) OVER (PARTITION BY idf.segment), 0) AS in_amt_vs_segment
        FROM gold.flow_features f
        JOIN gold.identity_features idf USING (account_id)
        JOIN gold.graph_features g USING (account_id)""")
        # weak labels: what a bank actually knows = accounts named in victim complaints
        con.execute("""
        CREATE TABLE gold.labels AS
        SELECT a.account_id,
               (a.account_id IN (SELECT reported_payee FROM bronze.complaints))::INT AS reported,
               (SELECT COUNT(*) FROM bronze.complaints c WHERE c.reported_payee = a.account_id) AS n_complaints
        FROM silver.accounts a""")
        rec["rows"]["gold.features"] = con.execute("SELECT COUNT(*) FROM gold.features").fetchone()[0]
        rec["rows"]["accounts named in complaints"] = con.execute("SELECT SUM(reported) FROM gold.labels").fetchone()[0]
        _check(rec, con, "features: no NaN in model inputs",
               "SELECT COUNT(*) FROM gold.features WHERE isnan(pass_through) OR isnan(fast_fwd_ratio) OR isnan(pagerank)")

    run.save(os.path.join(data_dir, "pipeline_run.json"))
    return con
