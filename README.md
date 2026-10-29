# RingBreaker

**Mule-network detection and AI-assisted investigation for Indian digital payments.**

**Live demo:** https://keshava2005.github.io/ringbreaker/ (later https://keshava.is-a.dev) ·
**Architecture:** [architecture.html](https://keshava2005.github.io/ringbreaker/architecture.html) ·
**Deployed on AWS:** https://ringbreaker.keshava.is-a.dev ([how](deploy/README.md))

Fraudsters don't keep stolen money in one account. Victims of investment scams, "digital arrest"
calls and fake loan apps pay into **money-mule accounts**, which forward the money within minutes
through more mules and cash it out at ATMs or through crypto OTC desks. Banks only learn about the
*first* account, and only when a victim files a complaint, often days later.

In 2026 the Finance Ministry asked banks to adopt RBI's MuleHunter.AI, and I4C signed an MoU with
RBI Innovation Hub to detect and freeze mule accounts with AI. RingBreaker is a working, end-to-end
version of that problem: a data pipeline, a mule-risk model, graph analytics that follow the money,
case management, and an LLM investigator that drafts the Suspicious Transaction Report.

> All data is synthetic. Banks, accounts, devices and people are fictional.

## Results (on the generated dataset)

| | |
|---|---|
| Transactions scanned | 6,87,237 UPI / IMPS over 61 days, 15,068 accounts |
| Mule rings planted / recovered as cases | 28 / 28, mean case purity 100% |
| Model PR-AUC vs threshold rules | **0.94 vs 0.61** |
| Precision at 300 alerts | 73% model vs 54% rules |
| Mules found that no victim ever reported | 79, including 64 cash-out accounts found only by following the money |
| Victim money traced | ₹4.42 crore, ₹30 lakh still recoverable |

The data is synthetic and the rings are planted, so these numbers are optimistic. What carries over
is the method: weak labels, ring-grouped validation, graph expansion and per-typology recall.

## Architecture

See `docs/architecture.html` for the full diagrams: the as-built system, the production design on AWS
(real-time scoring with Kinesis, Flink and Redis; batch learning with an Iceberg lake, Athena, Neptune and
SageMaker; a case path with Bedrock and Aurora), and the latency argument behind it.

## What's inside

```text
simulate  ->  bronze  ->  silver  ->  gold features  ->  model  ->  detect + expand  ->  cases  ->  investigator
(Python)      (DuckDB)    (11 DQ checks)  (SQL + graph)   (LightGBM)  (graph)            (Louvain)   (LLM + guardrails)
```

**1. A realistic payment world** (`simulate.py`)
- Normal life: salaries, rent, merchant spend, P2P, gig payouts, parents funding students, ATM use.
- Hard negatives that look like mules: employers (huge fan-out), merchants (fan-in plus daily sweeps),
  traders who collect and pay suppliers the same day, students who pay college fees within the hour,
  trip organisers, families sharing one phone, new customers, phone upgrades.
- Four mule typologies: layered fan-out, device farms, dormant-account takeover, circular layering.
- NCRP-style complaints: only ~40% of victims report, hours to days later, naming only the first mule.

**2. Medallion pipeline in SQL** (`pipeline.py`)
- Bronze → silver with 11 data-quality checks (duplicates, orphans, time window, nulls, failure rate).
- Gold features in DuckDB SQL, portable to BigQuery, Athena or Snowflake: an **ASOF join** for
  "time from credit to next debit", **range windows** for the busiest hour, dormancy gaps,
  pass-through, cash-out share, device and network sharing.
- Graph features in NetworkX: PageRank, money from and to fast-forwarders, large-value cycles.

**3. Mule-risk model** (`model.py`)
- LightGBM trained on **weak labels** (complaint-named accounts only), evaluated on hidden ground truth.
- 5-fold **GroupKFold by ring**, so no ring is in training and test at the same time.
- Compared with a threshold-rules baseline. Reports PR-AUC, precision and recall at alert budgets,
  per-typology recall, and per-role recall.
- Per-account explanations from SHAP contributions.

**4. Detection and cases** (`cases.py`)
- Model alerts + cycle detection + **graph expansion**: accounts that receive most of their money from
  flagged accounts, or share a phone with them. This finds the cash-out layer complaints never name.
- Connected components over money flow and shared devices, split with Louvain where two rings are
  bridged, become cases with roles (collector, layer, aggregator, cash-out), typology, severity,
  victim money, cash-out and recoverable balance.

**5. AI investigator** (`investigator.py`)
- A tool-using LLM agent (Groq free tier by default; OpenRouter or local Ollama also work) with four
  tools: `case_summary`, `account`, `flows` and read-only `sql`.
- It writes a structured report: evidence, per-account freeze or monitor actions, next steps, and
  grounds of suspicion for an STR to FIU-IND.
- **Guardrails**: single read-only SELECTs on an allow-list of gold tables only. Bronze tables,
  ground truth, files and URLs are blocked. The agent never sees IPs or VPAs, text in the data is
  treated as untrusted, and every tool call is streamed to the UI for audit.
- Without an API key, a deterministic rule-based investigator produces the same report shape,
  clearly labelled as not AI.

**6. Analyst console** (`web/index.html` + `api.py`)
- **Command center**: KPIs in ₹ lakh/crore, a live network map of every ring, a priority queue, daily
  victim inflow vs cash-out, and how each ring was found.
- **Case view**: a money-flow graph (victims → collectors → layers → cash-out → ATM/crypto) with
  **time-lapse replay** of every transfer, member accounts, the investigator, an STR draft, and
  analyst actions (start, freeze, file STR, not fraud) with an audit history.
- **Account 360**: risk gauge, SHAP reasons, behaviour profile, devices, counterparties, transactions.
- **Model lab**: an alert-budget slider showing precision, recall, unreported mules found and
  analyst hours for model vs rules, plus PR curves, typology recall, drivers and score distribution.
- **Pipeline**: stage DAG with row counts, timings and every data-quality check.
- FastAPI backend with OpenAPI docs at `/docs`, Server-Sent Events for the investigator, and SQLite
  for analyst actions. The server binds to localhost and rejects foreign Host and Origin headers.

## Run it

```bash
python -m venv .venv && .venv\Scripts\activate        # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
python -m ringbreaker build                            # ~1.5 min: data, pipeline, model, cases
python -m ringbreaker serve                            # http://127.0.0.1:8000   API docs: /docs
```

On Windows you can double-click `start.bat` instead.

**Turn on the AI investigator (free).** Get a key at console.groq.com, copy `.env.example` to `.env`,
set `GROQ_API_KEY=...`, and restart. The investigator rate-limits itself to stay under the free
tier's tokens-per-minute cap, and caches responses so re-runs cost nothing.

Other commands:

```bash
python -m ringbreaker investigate CASE-20261005    # run the investigator in the terminal
python -m ringbreaker export-demo                  # one-file offline demo -> dist/ringbreaker-demo.html
python -m pytest -q                                # 13 tests: pipeline, model, cases, API, guardrails
docker compose up --build                          # same app in a container
```

## Repository map

```text
ringbreaker/
  simulate.py      synthetic payments world + planted mule rings + complaints
  pipeline.py      bronze/silver/gold in DuckDB SQL, DQ checks, graph features
  model.py         LightGBM on weak labels, GroupKFold by ring, rules baseline, SHAP reasons
  cases.py         detection, graph expansion, Louvain splitting, case facts, flow graphs
  investigator.py  LLM agent with tools and SQL guardrails, rule-based fallback, STR draft
  build.py         orchestrates everything, writes data/app/*.json
  store.py         read model + SQLite for analyst actions and investigations
  api.py           FastAPI routes, SSE stream, host/origin checks
  export.py        offline demo snapshot
web/index.html     the analyst console (vanilla JS + D3)
tests/             pytest suite
```

## Limitations and next steps

- Synthetic data makes the separation cleaner than real life. Next: noisier camouflage and
  adversarial mules that delay forwarding to evade dwell-time features.
- Batch scoring over a 61-day window. Next: streaming features on each credit (Kafka or Kinesis plus a
  feature store) so a debit freeze can land before the money leaves.
- Analyst decisions are stored but not yet fed back into training. Next: retrain on confirmed and
  rejected cases and track drift.
- Graph features are recomputed in full. Next: incremental graph updates and a GNN on the
  transaction graph.
