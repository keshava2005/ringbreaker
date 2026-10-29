# RingBreaker: interview notes

## 30-second pitch
"Stolen money in India moves through chains of mule accounts within minutes, and banks only hear
about the first account when a victim complains. I built RingBreaker end to end: a SQL pipeline over
6.9 lakh UPI/IMPS transactions, a model trained only on complaint labels, and graph analytics that
follow the money to the cash-out layer. It recovered all 28 planted rings as cases, including 79
mules nobody had reported. An LLM investigator then works each case and drafts the STR, and it is
fenced in with read-only SQL guardrails."

## Map to the JD
| JD line | Where it is |
|---|---|
| Build and maintain data pipelines | Bronze/silver/gold in DuckDB with 11 DQ checks and a run log rendered as a DAG |
| Strong SQL, complex schemas | ASOF join for dwell time, RANGE window for bursts, dormancy via LAG, feature store joins |
| Python, pandas/numpy | Simulator, graph features, case builder, FastAPI service |
| Experimentation, diagnostics | Alert-budget trade-off, model vs rules, per-typology and per-role recall |
| ML systems: outputs, metrics, deployment | Weak labels, GroupKFold by ring, PR-AUC, precision@k, SHAP reasons, saved model, API |
| Translate business questions | "How many analyst hours to catch X% of mules?" answered by the budget slider |
| AI coding frameworks / harnesses | Tool-using investigator with guarded SQL, streaming steps, audit trail |
| Cloud data platforms | SQL is portable to BigQuery/Athena; Docker image; CI builds the demo |

## Decisions to be ready to defend
- **Weak labels.** A bank only knows complaint-named accounts. Training on ground truth would
  overstate performance, so ground truth is used only to evaluate.
- **GroupKFold by ring.** Mules in one ring look alike. A random split leaks, so whole rings are held out.
- **PR-AUC, not accuracy.** Mules are about 1.6% of accounts, so accuracy would be about 98% for a
  model that flags nobody.
- **Graph expansion.** The model scores cash-out accounts low because they don't receive victim money
  directly. Following money out of flagged accounts found 64 of them.
- **Louvain splitting.** A false-positive "victim" account can bridge two rings into one component.
  Modularity splitting separates them.
- **Guardrails.** The LLM gets read-only, allow-listed gold tables. It cannot see ground truth, bronze
  tables or files, and every tool call is logged. Your security background is the point here.

## Honest limitations
- Synthetic data makes separation cleaner than reality, so expect lower precision on real data.
- Scoring is batch, but a freeze has to land within minutes. Next is streaming features on each credit.
- Analyst feedback is stored but not yet retrained on.

## Questions to ask them
- How do you get labels today: complaints, chargebacks, or analyst dispositions?
- What is the alert budget per analyst per day, and who owns the precision/recall trade-off?
- Do you score at transaction time or in batch?
