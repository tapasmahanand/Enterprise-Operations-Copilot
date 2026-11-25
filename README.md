# Kestrel Resolution Copilot

A multi-agent copilot for the tier-1 resolution desk of **Kestrel Bank**, a fictional Indian retail bank. Agents handling refund, card-dispute and unauthorised-transaction calls get a cited, RBI-aligned answer in seconds; anything that moves money is drafted, paused durably for supervisor approval, and fully traced.

> Status: **Milestone 2 of 4** (hero demo end to end, including restart-safe approval). See [docs/plan.md](docs/plan.md) and [docs/design.md](docs/design.md).

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

It checks: card blocked without approval → calculator gives zero liability and the shadow-credit due date (R17 regime, by transaction date) → shadow credit drafted for the *looked-up* amount → case pending → **app killed and restarted** → supervisor approves → case graph resumes from its Postgres checkpoint → credit posted exactly once → SMS drafted → a second decision gets 409.

`/health` returns 200 only when the database is reachable and the knowledge base is loaded.

Offline / no key: set `EMBEDDING_PROVIDER=fake` in `.env` (lexical test embeddings; not for quality evaluation).

## What's here so far

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
