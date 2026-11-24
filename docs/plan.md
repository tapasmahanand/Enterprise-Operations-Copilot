# Kestrel Copilot: Build Plan (v1)

*Implements [design.md](design.md) v2 · 2026-11-24 · sized in hours, not dates*

Four milestones. Each one ends in something you can run and show. **Total ≈ 100 hours** (±25%).
At 20 h/week that's about 5 weeks; at 30 h/week about 3.5; at 40 h/week about 2.5.

Order rule: the **hero demo works end to end by the end of Milestone 2** (about 55 h in). Everything after that is breadth and evidence, so if time runs out you still have a working, demoable system.

## Schedule at 40 h/week (Mon–Fri, ~8 h/day, from Tue 24 Nov 2026)

| Milestone | Hours | Target finish |
|---|---|---|
| 1. Foundations and data | 22 | **Thu 26 Nov** |
| 2. Hero demo end to end | 33 (+8 buffer for the case-graph spike) | **Thu 3 Dec** ← go/no-go check |
| 3. Breadth and safety | 22 | **Tue 8 Dec** |
| 4. Evidence and packaging | 23 | **Fri 11 Dec** |
| Buffer (~20%) | ~20 | **Submit by Wed 16 Dec** |

**Go/no-go on Thu 3 Dec:** if the hero demo (with restart) isn't working by then, apply the cut list below right away, not in week 3. Weekends are left free on purpose. They're the real buffer if estimates slip, which they usually do with an unfamiliar framework.

A detailed, test-first task list for each milestone is written when that milestone starts, not all up front. The milestones are separate pieces of work, and a 100-hour task list written today would be stale by week 2.

---

## Milestone 1: Foundations and data (~22 h)

**First hour, before anything else:**
- Check every RBI figure the policies will use against the source circulars (customer liability 2017; failed-transaction TAT 2019; Integrated Ombudsman 2021), and check whether any of them has since been superseded. Record each figure with its source in `docs/rbi-sources.md`.
- Check that "Kestrel Bank" isn't a real bank in India (RBI's list of banks, plus a web search). Rename it if it is.

**Then:**
- Repo, `docker-compose.yml` with Postgres + pgvector and a FastAPI app with `/health`. `.env.example`.
- Schema: the brief's 5 tables plus `jobs`, `run_events` and the mock-system tables.
- **Synthetic corpus**: ≥25 Kestrel docs with front-matter (`effective_from`, `supersedes`, `status`), including the superseded-policy trap and the injection trap. Drafting with an LLM is fine, but every RBI figure must be checked by hand against `rbi-sources.md`.
- **Approval matrix and calculator rules** as one versioned YAML file. **Liability/TAT calculator** as a pure function with a table-driven unit test (day 3 vs day 4, a holiday in between, each channel).
- BANKING77 normalisation, the in-scope intent list (~15–20), and a held-out routing set. Enron ~2k-email sample.
- Ingest service: chunk → metadata → embed → upsert, idempotent. Seed the mock tickets, transactions and cards, including the ₹18,450 hero transaction.

**Done when:** `docker compose up` loads everything, a raw similarity query for "unauthorised card transaction liability" returns the *current* policy chunk, and the calculator tests pass.

## Milestone 2: Hero demo end to end (~33 h). This is the demoable core.

- **Chat graph**: compliance_in (PII masking for card/PAN/Aadhaar/phone), memory_load, Supervisor (structured output), Retrieval (rewrite + filters), Tool Execution (`block_card`, `transaction_lookup`, `ticket_lookup`, calculator), Composer with citations and rule ids, memory_save.
- **SSE streaming** of node-level progress plus tokens.
- **Drafts and case graph**: `POST /drafts/{id}/submit` → case graph → `interrupt()` + approvals row with ₹ tier and due date → `POST /approvals/{id}`, idempotent (409 on a second decision) → `submit_credit` → notification draft. Postgres checkpointer.
- **Minimal UI**: an Agent tab (input, live progress, answer, citations, script, Send button, case status) and a Supervisor tab (queue sorted by time to breach, evidence, approve/reject).
- **Self-hosted Langfuse** in compose, with a span per node, plus the `run_events` fallback.

**Done when:** the full hero demo from design §1 runs on a fresh `compose up`, *including killing `app` mid-approval and resuming*, and the trace shows every step with cost.

## Milestone 3: Breadth and safety (~22 h)

- **Guardrails**: injection heuristics on user input *and* retrieved chunks, output compliance (citation check, unapproved-promise block, no unmasked PII), and refusals from the matrix.
- **Remaining actions and tools**: fee reversal, chargeback, escalate complaint, `policy_lookup`, `org_lookup`, `add_ticket_note`. Fault injection on one tool to show retries. The secondary "charged twice" demo.
- **Async worker** (Postgres queue): the Enron compliance scan and the TAT deadline sweeper.
- Background memory summary, and the step/cost budget.
- Unit tests for each guard and tool. Integration tests for all 5 workflows, restart, and the duplicate decision.

**Done when:** all 5 required workflows pass their integration tests and appear as traces.

## Milestone 4: Evidence and packaging (~23 h)

- `make eval`: ≥20 scenarios, routing (intent + route accuracy), groundedness judge, and the §5.11 metrics → `reports/eval.md`.
- **Model comparison** table (2 models: accuracy, latency, cost), **retrieval ablation** (vector vs hybrid vs hybrid+filters; recall@k, MRR), and a **blameless postmortem** of one real failure.
- Clean-machine test: fresh clone → `docker compose up --build` → README readiness checks pass.
- README, architecture diagram, technical write-up (including the "why some agents are code" argument), and a 10–15 min demo video.

**Done when:** every line of the brief's final submission checklist is ticked.

---

## If time runs short, cut in this order
1. Third retrieval-ablation variant (keep vector vs hybrid)
2. Fault-injection demo
3. Secondary "charged twice" demo
4. Background memory summary (keep entity memory)
5. Model comparison down to Supervisor only

Never cut: the restart-safe approval, the traces, the eval report, or clean-machine startup. Those carry most of the points.

## Biggest risks
- **The case graph and restart behaviour** (Milestone 2): the part most likely to take twice as long. Spike it in the first 3 hours of the milestone.
- **Self-hosted Langfuse weight** (several containers): if it breaks clean-machine startup, fall back to Langfuse Cloud plus `run_events` and say so in the README.
- **Wrong RBI figures in LLM-drafted docs:** a banking-domain reviewer will spot them. That's why they're checked first.
