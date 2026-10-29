"""Mule-risk model.

Training labels are *weak*: an account is positive only if a victim named it in a
complaint (what a bank really knows). Evaluation uses the hidden ground truth, so
the metrics answer the real question: how many mules - reported or not - do we
catch for a given analyst workload?

* 5-fold GroupKFold out-of-fold scoring. Groups are mule rings, so no ring is in
  train and test at the same time (no leakage through near-duplicate accounts).
* LightGBM vs a hand-written rules baseline, scored on the same accounts.
* Per-account explanations from LightGBM's SHAP contributions (pred_contrib).
"""
from __future__ import annotations

import json
import os

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, precision_recall_curve, roc_auc_score
from sklearn.model_selection import GroupKFold

FEATURES = [
    "in_cnt", "in_amt", "uniq_in", "max_in", "in_days", "night_in_ratio", "out_cnt", "out_amt", "uniq_out",
    "pass_through", "cash_out_ratio", "crypto_ratio", "merchant_spend_ratio", "median_dwell_min", "fast_fwd_ratio",
    "max_in_1h", "max_gap_days", "account_age_days", "kyc_min", "age", "max_device_share", "max_ip_share",
    "n_devices", "new_device_in_window", "pagerank", "in_degree", "out_degree", "in_from_fast_fwd",
    "out_to_fast_fwd", "in_large_cycle", "in_amt_vs_segment", "seg_code",
]
FEATURE_LABELS = {
    "in_cnt": "Inbound credits", "in_amt": "Total inbound ₹", "uniq_in": "Unique senders", "max_in": "Largest credit",
    "in_days": "Days with credits", "night_in_ratio": "Night-time credits", "out_cnt": "Outbound debits",
    "out_amt": "Total outbound ₹", "uniq_out": "Unique recipients", "pass_through": "Pass-through ratio",
    "cash_out_ratio": "ATM cash-out share", "crypto_ratio": "Crypto OTC share", "merchant_spend_ratio": "Merchant spend share",
    "median_dwell_min": "Median dwell time", "fast_fwd_ratio": "Forwarded within 1h", "max_in_1h": "Credits in busiest hour",
    "max_gap_days": "Longest dormancy", "account_age_days": "Account age", "kyc_min": "Minimum-KYC account",
    "age": "Holder age", "max_device_share": "Accounts on same device", "max_ip_share": "Accounts on same network",
    "n_devices": "Devices used", "new_device_in_window": "New device this period", "pagerank": "Network centrality",
    "in_degree": "In-degree", "out_degree": "Out-degree", "in_from_fast_fwd": "Funds from fast-forwarders",
    "out_to_fast_fwd": "Funds to fast-forwarders", "in_large_cycle": "In a large-value cycle",
    "in_amt_vs_segment": "Inflow vs peer segment", "seg_code": "Customer segment",
}
SEGMENTS = ["salaried", "student", "self_employed", "gig_worker", "homemaker", "retired", "merchant",
            "merchant_settlement", "corporate"]
PARAMS = dict(objective="binary", learning_rate=0.05, num_leaves=15, min_child_samples=10, feature_fraction=0.8,
              bagging_fraction=0.8, bagging_freq=1, lambda_l2=1.0, verbose=-1, seed=7)


def rules_score(df: pd.DataFrame) -> pd.Series:
    """What many banks run today: a handful of threshold rules, points added up."""
    s = (
        2.0 * ((df.fast_fwd_ratio > 0.6) & (df.pass_through > 0.8)).astype(float)
        + 1.0 * (df.median_dwell_min < 120).astype(float)
        + 1.0 * (df.account_age_days < 90).astype(float)
        + 1.0 * (df.kyc_min == 1).astype(float)
        + 1.5 * (df.max_device_share >= 4).astype(float)
        + 1.0 * (df.cash_out_ratio > 0.5).astype(float)
        + 1.0 * (df.uniq_in >= 10).astype(float) * (df.segment.isin(["student", "homemaker", "gig_worker"])).astype(float)
    )
    return s / 8.5


def train_and_score(con, data_dir: str = "data") -> pd.DataFrame:
    df = con.execute("""
        SELECT f.*, l.reported, l.n_complaints, g.is_mule, g.ring_id, g.typology, g.role
        FROM gold.features f JOIN gold.labels l USING (account_id) JOIN eval.ground_truth g USING (account_id)
    """).df()
    df["seg_code"] = df.segment.map({s: i for i, s in enumerate(SEGMENTS)}).fillna(-1)
    X = df[FEATURES].astype(float).values
    y = df.reported.values
    # group = ring for mules, otherwise the account itself
    groups = np.where(df.ring_id.notna(), df.ring_id.astype(str), df.account_id)

    oof = np.zeros(len(df))
    contrib = np.zeros((len(df), len(FEATURES)))
    importances = np.zeros(len(FEATURES))
    for fold, (tr, te) in enumerate(GroupKFold(n_splits=5).split(X, y, groups)):
        m = lgb.train(PARAMS, lgb.Dataset(X[tr], y[tr]), num_boost_round=300)
        oof[te] = m.predict(X[te])
        contrib[te] = m.predict(X[te], pred_contrib=True)[:, :-1]
        importances += m.feature_importance("gain")
    df["model_score"] = oof
    df["rules_score"] = rules_score(df)

    # top reasons per account (positive SHAP contributions)
    reasons = []
    for row in contrib:
        idx = np.argsort(-row)[:4]
        reasons.append([{"feature": FEATURES[i], "label": FEATURE_LABELS[FEATURES[i]], "impact": round(float(row[i]), 3)}
                        for i in idx if row[i] > 0])
    df["reasons"] = [json.dumps(r) for r in reasons]

    # final production model on all data (for the API's live re-scoring)
    final = lgb.train(PARAMS, lgb.Dataset(X, y), num_boost_round=300)
    os.makedirs(os.path.join(data_dir, "model"), exist_ok=True)
    final.save_model(os.path.join(data_dir, "model", "mule_lgbm.txt"))

    imp = pd.DataFrame({"feature": FEATURES, "label": [FEATURE_LABELS[f] for f in FEATURES],
                        "gain": importances / importances.sum()}).sort_values("gain", ascending=False)
    df.attrs["importance"] = imp
    return df


def evaluate(df: pd.DataFrame, budgets=(25, 50, 100, 150, 200, 300, 400, 600, 800, 1000)) -> dict:
    """Metrics against hidden ground truth. Money-weighted recall counts victim ₹ flowing into caught accounts."""
    truth = df.is_mule.values
    out = {"n_accounts": int(len(df)), "n_mules": int(truth.sum()), "n_reported": int(df.reported.sum()),
           "n_unreported_mules": int(((df.is_mule == 1) & (df.reported == 0)).sum())}
    for name in ("model_score", "rules_score"):
        s = df[name].values
        p, r, _ = precision_recall_curve(truth, s)
        keep = np.linspace(0, len(p) - 1, 120).astype(int)
        order = np.argsort(-s)
        rows = []
        for k in range(10, 1201, 10):
            top = order[:k]
            tp = truth[top].sum()
            rows.append({"k": k, "precision": round(tp / k, 4), "recall": round(tp / truth.sum(), 4),
                         "unreported_found": int(((df.is_mule.values[top] == 1) & (df.reported.values[top] == 0)).sum()),
                         "inflow_caught": float(df.in_amt.values[top][truth[top] == 1].sum())})
        out[name] = {
            "pr_auc": round(float(average_precision_score(truth, s)), 4),
            "roc_auc": round(float(roc_auc_score(truth, s)), 4),
            "pr_curve": [{"precision": round(float(p[i]), 4), "recall": round(float(r[i]), 4)} for i in keep],
            "at_k": rows,
            "budgets": [next(x for x in rows if x["k"] >= b) for b in budgets],
        }
    # recall by typology at a 300-alert budget
    top = set(df.sort_values("model_score", ascending=False).account_id.head(300))
    top_r = set(df.sort_values("rules_score", ascending=False).account_id.head(300))
    mules = df[df.is_mule == 1]
    out["by_typology"] = [
        {"typology": t, "mules": int(len(g)), "model_recall": round(g.account_id.isin(top).mean(), 3),
         "rules_recall": round(g.account_id.isin(top_r).mean(), 3)}
        for t, g in mules.groupby("typology")]
    out["by_role"] = [
        {"role": t, "mules": int(len(g)), "model_recall": round(g.account_id.isin(top).mean(), 3),
         "reported_share": round(g.reported.mean(), 3)}
        for t, g in mules.groupby("role")]
    out["importance"] = df.attrs["importance"].head(15).to_dict("records")
    return out
