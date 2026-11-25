# Kestrel Resolution Copilot

A multi-agent copilot for the tier-1 resolution desk of **Kestrel Bank**, a fictional Indian retail bank. Agents handling refund, card-dispute and unauthorised-transaction calls get a cited, RBI-aligned answer in seconds; anything that moves money is drafted, paused durably for supervisor approval, and fully traced.

> Status: **Milestone 4 of 4** (evaluation and packaging). Design: [docs/design.md](docs/design.md) · Architecture and why most agents are code: [docs/architecture.md](docs/architecture.md) · Decisions: [docs/decisions.md](docs/decisions.md) · Postmortem: [docs/postmortem-invented-citation.md](docs/postmortem-invented-citation.md).

![Hero case at identity level 2: ₹18,450 shadow credit drafted for team-lead approval](docs/screenshots/03-hero-level-2-draft-18450.png)

## Quick start

```bash
cp .env.example .env          # add OPENAI_API_KEY
docker compose up --build     # postgres → one-shot ingest → app, plus self-hosted Langfuse
curl localhost:8000/health    # {"status": "ready", ...}
```

- Agent console and supervisor queue: http://localhost:8000
- Traces: http://localhost:3000 (Langfuse; log in as `demo@kestrel.example` / `kestrel-demo`). Every step is also in the `run_events` table and shown in the console's Trace panel, so the demo never depends on Langfuse.

## Hero demo (unauthorised ₹18,450 card payment)

In the UI: press **Ask copilot** (the sample text is pre-filled), then **Send to supervisor**, restart the app, switch to **Supervisor queue** and **Approve**.

Or scripted, with a hard kill of the app between submit and approval:

```bash
python scripts/hero_demo.py --restart "docker compose kill app && docker compose up -d app"
```

Every request carries the caller's identity-verification level (`idv_level`, KB-SOP-IDV). The console starts every call at **level 0 (not verified)**; at level 0 the copilot answers policy questions but will not block a card or draft a credit. Set level 2 before running the hero demo. The level is recorded in the trace and on the draft, and enforced in code (card block ≥ 1; any credit, refund or fee reversal ≥ 2 plus complete KYC).

All five workflows from the brief, against the running stack:

```bash
python scripts/workflows_e2e.py     # RAG, tool-assisted, approval, async job, guardrails (16 checks)
```

| Workflow | What runs |
|---|---|
| RAG | policy question → date-filtered retrieval → cited answer |
| Tool-assisted | "charged twice" → transaction lookup finds the posted pair (KB-POL-DUP) → refund drafted |
| Approval (HITL) | case paused in a Postgres checkpoint → tier-checked approval → resume, credit posted once, 409 on repeat |
| Async job | `POST /jobs` (202) → `worker` runs the Enron compliance scan; TAT sweeper runs every 5 min |
| Guardrailed | input injection blocked, trap lines stripped from retrieved text, matrix refusals KB-REF-1/2, output check |

It checks: card blocked without approval → calculator gives zero liability and the shadow-credit due date (R17 regime, by transaction date) → shadow credit drafted for the *looked-up* amount → case pending → **app killed and restarted** → supervisor approves → case graph resumes from its Postgres checkpoint → credit posted exactly once → SMS drafted → a second decision gets 409.

`/health` returns 200 only when the database is reachable and the knowledge base is loaded.

Offline / no key: set `EMBEDDING_PROVIDER=fake` in `.env` (lexical test embeddings; not for quality evaluation).

## Results (Milestone 4)

Full numbers: [reports/eval.md](reports/eval.md) (run 2, last complete run) and
[reports/eval-run3-partial.md](reports/eval-run3-partial.md) (run 3: the production half
finished; the gpt-5.4 half stopped when the API account ran out of credits). Run it yourself
with `make eval` (about 30 min, roughly ₹230 at list prices, ₹88/USD assumed).

**Model comparison** (Supervisor + Composer; 25 scenarios × 3 repeats, run 2):

| | gpt-5.4-mini (production) | gpt-5.4 |
|---|---|---|
| Scenario runs passing | 67/75 (89%) | 69/75 (92%) |
| Groundedness: supported claims (LLM judge) | 89% | 92% |
| Latency p50 / p95 | 4.1 s / 4.8 s | 5.3 s / 6.1 s |
| Cost per copilot answer | ₹0.23 | ₹0.76 |
| BANKING77 intent accuracy (n=200) | 78% | 78% |
| Out-of-scope correctly refused | 77% | 68% |

The bigger model is about 3× the cost for a 3-point gain that is inside run-to-run noise
(routing moves ±3 points between identical runs), so production stays on mini.
**Held-out scenarios** (written after run 2, never tuned on): 6/8 pass all repeats on mini
(run 3), against 21/25 on the tuned set.

**Ops and compliance** (production config): 0 unapproved promises reached the agent or were
even attempted in the raw stream; 0 invented citations; 100% of drafted actions carry a
complete audit trail; the TAT sweeper caught 6/6 breaches in a 60-case simulation, warning
a median 2 days ahead. The 10% deadline-breach rate depends on an **assumed** approval delay;
it measures staffing, not the software.

**Compliance scan accuracy** (226 hand-labelled Enron emails, stratified, population-weighted;
[evals/compliance/README.md](evals/compliance/README.md)): precision **71%** (95% CI 38–89%),
recall **40%** (CI 13–100%) on strict labels, up from 38% / 20% before three fixes. The fixes
were made on the same sample, so these are in-sample numbers and optimistic. The scan misses
more than it catches; treat it as a triage aid, not a control.

**Retrieval ablation** (34 gold queries): metadata filters are what matter. Without them the
superseded policy version ranks in the top 3 for 8/8 version-sensitive queries; with them, 0/8.
Hybrid search adds one query at rank 1 (MRR 0.90 → 0.92), not enough to justify a second index
yet (D45).

**Known weak spots:** (1) the mini model sometimes refuses in-scope questions as out of scope,
and the same note flips between runs (S15, H05); (2) fee-reversal drafting depends on the LLM's
route choice (S08b fails on both models); (3) gpt-5.4-nano, used for query rewrite and the
borderline prompt-injection judge, is deprecated and needs replacing.

**Screenshots** ([docs/screenshots](docs/screenshots)): a fresh console starts at level 0 and
will neither look up the account nor block the card (01, 02); at level 2 the hero case drafts
the ₹18,450 credit for team-lead approval (03), goes to the supervisor queue (04, 05) and is
approved with the credit posted once (06).

**Demo video:** to be recorded by the project owner (not producible from this environment).

## What's where

| Piece | Where |
|---|---|
| Rules (approval matrix, liability regimes, failed-transaction TAT) | `rules/kestrel_rules.yaml` |
| RBI figures and sources, rule by rule | `docs/rbi-sources.md` |
| Liability / TAT calculator (pure functions) | `kestrel/rules/calculator.py` |
| Approval matrix | `kestrel/rules/approval.py` |
| Synthetic policy corpus (29 docs, incl. a superseded version and two prompt-injection traps) | `data/corpus/` |
| BANKING77 (routing labels) and in-scope intents | `data/banking77/`, `rules/intents.yaml` |
| Schema | `db/schema.sql` |
| Ingest (mock bank data, BANKING77, chunk → embed → PGVector) | `kestrel/ingest/` |
| Decision log | `docs/decisions.md` |
| Evaluation harness, scenarios, gold sets, labelled compliance sample | `evals/` |
| Eval and compliance reports | `reports/` |

## Tests

```bash
pip install -e '.[dev]'
pytest                         # unit tests; DB tests auto-skip without Postgres
docker run -d -p 5432:5432 -e POSTGRES_USER=kestrel -e POSTGRES_PASSWORD=kestrel -e POSTGRES_DB=kestrel pgvector/pgvector:pg16
pytest -m db                   # ingest, retrieval, chat graph, restart-safe approval (throwaway database)
```

## Data sources
- BANKING77 — PolyAI, CC BY 4.0.
- Enron email corpus (sample) — CMU; used only by the batch compliance scan.
- Kestrel policies — written for this project, aligned with RBI directions listed in `docs/rbi-sources.md`. Kestrel Bank, its staff and customers are fictional.
