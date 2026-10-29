"""End-to-end tests on a small world: pipeline, model quality, case recovery, API, guardrails."""
import os

import pytest

from ringbreaker.build import build
from ringbreaker.investigator import inr_in, validate_sql


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory):
    d = str(tmp_path_factory.mktemp("rb"))
    build(d, fast=True, log=lambda *_: None)
    return d


def test_pipeline_quality_checks_pass(data_dir):
    import json
    run = json.load(open(os.path.join(data_dir, "pipeline_run.json")))
    checks = [c for s in run["stages"] for c in s["checks"]]
    assert checks and all(c["status"] in ("pass", "warn") for c in checks)
    assert all(s["status"] != "failed" for s in run["stages"])


def test_model_beats_rules_and_cases_recover_rings(data_dir):
    import json
    m = json.load(open(os.path.join(data_dir, "app", "model.json")))
    assert m["model_score"]["pr_auc"] > m["rules_score"]["pr_auc"]
    assert m["cases"]["rings_found"] >= 0.8 * m["cases"]["rings_total"]
    assert m["cases"]["mean_purity"] >= 0.8


def test_ground_truth_never_reaches_features(data_dir):
    import duckdb
    con = duckdb.connect(os.path.join(data_dir, "warehouse.duckdb"), read_only=True)
    cols = {r[0] for r in con.execute("DESCRIBE gold.features").fetchall()}
    assert not cols & {"is_mule", "ring_id", "typology", "role", "is_victim"}


@pytest.mark.parametrize("sql,ok", [
    ("SELECT * FROM gold.scores LIMIT 5", True),
    ("WITH x AS (SELECT * FROM gold.edges) SELECT COUNT(*) FROM x", True),
    ("SELECT * FROM eval.ground_truth", False),
    ("SELECT * FROM bronze.accounts", False),
    ("DROP TABLE gold.scores", False),
    ("SELECT 1; DELETE FROM gold.scores", False),
    ("SELECT * FROM read_parquet('data/raw/ground_truth.parquet')", False),
    ("SELECT * FROM scores", False),
])
def test_investigator_sql_guard(sql, ok):
    assert (validate_sql(sql) is None) == ok


def test_indian_number_format():
    assert inr_in(2961382) == "₹29,61,382"
    assert inr_in(999) == "₹999"


def test_api_end_to_end(data_dir):
    from fastapi.testclient import TestClient

    from ringbreaker.api import create_app
    c = TestClient(create_app(data_dir), base_url="http://127.0.0.1")
    cases = c.get("/api/cases").json()
    assert cases
    cid = cases[0]["case_id"]
    detail = c.get(f"/api/cases/{cid}").json()
    assert detail["graph"]["nodes"] and detail["members"]
    acc = detail["members"][0]["account_id"]
    assert c.get(f"/api/accounts/{acc}").status_code == 200
    assert c.post(f"/api/cases/{cid}/actions", json={"action": "freeze", "note": "test"}).json()["status"] == "Frozen"
    assert c.get(f"/api/cases/{cid}").json()["status"] == "Frozen"
    assert c.post(f"/api/cases/{cid}/actions", json={"action": "delete_everything"}).status_code == 422
    stream = c.get(f"/api/cases/{cid}/investigate").text
    assert '"type": "final"' in stream
    # path tricks and foreign hosts are refused
    assert c.get("/api/accounts/..%2F..%2Fetc").status_code == 404
    evil = TestClient(create_app(data_dir), base_url="http://evil.example")
    assert evil.get("/api/cases").status_code == 403
    assert c.post(f"/api/cases/{cid}/actions", json={"action": "start"}, headers={"Origin": "http://evil.example"}).status_code == 403
