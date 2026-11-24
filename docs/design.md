# Tier-1 Resolution Copilot: Design

*Enterprise Operations Copilot capstone · design v2 · 2026-11-24 (v2: Indian bank, ₹, RBI-aligned policies, AI-engineer evidence, ops/compliance metrics)*
*Companion to the course brief: `capstone-architecture-brief.md` (same folder). Where this doc is silent, the brief's requirements apply.*

Items marked **[call]** are the designer's own decisions, not confirmed by Tapas; they can be overturned.

---

## 1. Intent and the why

Build the 4-week Agentic AI bootcamp capstone as a believable, interview-defensible internal tool for a regulated bank, not a demo chatbot. The bar (from the brief): a reviewer runs `docker compose up --build`, sends a request, watches it stream, sees it pause for approval, resumes it, and opens a trace that explains what happened and what it cost.

### Problem framing (confirmed by Tapas)

**Tier-1 resolution desk at a retail bank.** Agents handling refund, reimbursement and card-dispute calls lose minutes per call hunting through policy documents while the customer is on hold, and sometimes promise customers things only a supervisor can approve.

Three measurable pains:

1. **Slow, inconsistent answers.** The copilot classifies the intent, retrieves the right policy and returns a cited answer in seconds.
2. **Unsafe promises.** The copilot drafts the case, pauses it durably for supervisor approval, and gives the agent the correct script ("we'll confirm within 24h").
3. **No audit trail.** Every answer, tool call and approval is traceable, which a regulated bank needs.

It is a **staff tool, not customer-facing**. The datasets are customer messages read by staff.

### Setting and audience (confirmed by Tapas)
- **The bank is fictional:** "Kestrel Bank", a large Indian private-sector retail bank **[call on the name; check it isn't a real bank before use]**. **No real bank is named anywhere** in the repo, docs, demo or write-up.
- **All money is in rupees (₹)**, and the policy rulebook is **RBI-aligned** (customer-liability rules, failed-transaction turnaround times, Integrated Ombudsman).
- **Target reader:** a hiring manager in **banking ops or compliance tech** evaluating an **AI engineer**. Incident response is *not* built as a feature; it is addressed by a blameless postmortem in the evaluation report (§5.9).

### Personas
- **Tier-1 agent** (primary): customer on hold, needs an answer fast. Latency and streaming progress are core features.
- **Supervisor / team lead**: works an approval queue after the call (minutes to hours later). Approves or rejects drafted actions.
- **Compliance officer** (minor): launches the batch compliance scan (async workflow).

### Demo story (hero path)
Customer: "There's a ₹18,450 card payment I never made, it went through yesterday." → agent pastes it (card number in the text is masked by Compliance) → copilot streams `checking input → classifying → blocking card → retrieving customer-liability policy → looking up transactions → calculating liability → drafting shadow credit` → card blocked immediately (no approval needed) → calculator: reported within 3 working days ⇒ **zero customer liability**, shadow credit due within 10 working days (date shown) → cited answer + script for the agent → agent clicks **Send to supervisor** → **server restarted** → supervisor (approval tier by ₹ amount) approves → case graph resumes, shadow credit "posted" (mock), customer notification drafted → reviewer opens the trace.

Secondary demo: "I was charged twice" (duplicate charge → refund via approval), used for the tool-assisted and RAG workflows.

## 2. Goals, non-goals, anti-goals

**Goals**
- Satisfy every required item in the brief (stack, 7 agent roles, 5 workflows, ≥5 tools, 3 datasets, ≥20 evals, 7 deliverables).
- Prioritise the three 15-point rubric areas: multi-agent architecture, RAG + PGVector, observability + evals.
- p50 end-to-end latency for a non-approval question under ~6 s, with first streamed progress event < 1 s **[call]**.
- Agent boundaries that are each defensible in one sentence ("what breaks if deleted").
- **AI-engineer evidence** (Tapas): eval harness runnable in one command, a two-model cost/accuracy comparison, a retrieval ablation (§5.9).
- **Ops/compliance metrics** a banking manager already tracks, reported alongside AI metrics (§5.11).
- **LLM for judgement, code for rules:** the model never does date or ₹ arithmetic; a deterministic calculator does (§5.3).

**Non-goals**
- Customer-facing chat. Real banking integrations. Incident-response features (covered only by the postmortem, §5.9). Authentication/SSO beyond a hard-coded role header **[call]**. Polished frontend.
- Fine-tuning; multi-tenant; horizontal scaling.

**Anti-goals (failure even if it "works")**
- An approval gate that doesn't really pause, or loses state on restart.
- An "async" job that blocks the request.
- Answers not grounded in retrieved chunks, or citations that don't support the claim.
- Decorative agents: nodes that exist only to tick a box.
- A README that needs the author's machine.

## 3. Constraints

- Mandatory stack: LangGraph, FastAPI, PostgreSQL + PGVector, OpenAI API, Langfuse or LangSmith, Docker Compose.
- Embeddings: `text-embedding-3-small` (brief's recommendation). Chunks 400–800 tokens with overlap; metadata on every chunk.
- One Postgres is the system of record for knowledge *and* workflow state.
- Timeline: 4 weeks (brief's milestones). Solo builder. Weekly hours and deadline not yet given.
- No real bank names; all amounts in ₹; RBI figures in the synthetic policies must be checked against the source circulars when the docs are written (RBI DBR.No.Leg.BC.78/09.07.005/2017-18 on customer liability; RBI DPSS circular of 2019 on harmonised TAT for failed transactions; Integrated Ombudsman Scheme 2021), and the policy docs say "aligned with", not "is".

## 4. Approaches considered

| | Approach | Trade-off | Verdict |
|---|---|---|---|
| A | Literal brief: 7 LLM-backed agents, Supervisor calls each | 6–9 LLM calls/request, 15–30 s on hold; "decorative agent" risk | Rejected |
| **B** | **Mixed graph: all 7 roles are nodes with contracts; only Supervisor, Retrieval (query rewrite) and Composer call an LLM; Compliance, Approval, Memory, Tool Execution are deterministic code** | 2–3 LLM calls, ~3–6 s; deterministic guardrails easy to test; must justify in write-up why some "agents" are code | **Chosen by Tapas** |
| C | Single ReAct agent with tools + guard nodes | Fastest to build; fails multi-agent criterion, routing hard to grade | Rejected |

The write-up must argue for B explicitly: "agent" = a unit with owned state, a contract and independent failure, not necessarily an LLM.

## 5. Decided design

### 5.1 Graph (LangGraph, Postgres checkpointer)

Two graphs, two kinds of thread **[call, from builder review]**. The agent must get the cited answer and script while the customer is on hold, but approval can take hours, so the pause cannot sit inside the chat run.

**Chat graph** (`thread_id` = session id; one run per agent message, never paused):

```
POST /chat ─► compliance_in ──block──► blocked_response ─► END
                  │ allow/redact
                  ▼
              memory_load
                  ▼
              supervisor  (LLM, structured output)
     ┌────────────┼──────────────┬───────────────┐
     ▼ rag        ▼ tools        ▼ action         ▼ async (compliance role only)
  retrieval    retrieval      retrieval        enqueue_job
     │          + tool_exec   + tool_exec(read)    │
     │            │          draft_action ─► (agent clicks Send) ─► case graph
     ▼            ▼             ▼                  ▼
                  compliance_out ─► composer (LLM) ─► memory_save ─► stream END
```

**Case graph** (`thread_id` = case id; one per drafted action):

```
start(case, drafted action, evidence) ─► approval_gate: interrupt() + approvals row (pending)
      │ POST /approvals/{id}
      ├─ approved ─► tool_exec(write, e.g. submit_credit) ─► draft_customer_notification ─► case CLOSED
      └─ rejected ─► no side effect, reason logged ─► case CLOSED
```

- The agent's chat run finishes immediately with: cited answer, the script line ("we'll confirm within 24h"), and the case id with status `pending_approval`. The agent can keep chatting in the same session.
- Resumed output goes to the **case record** (status, receipt, notification draft) and the **supervisor queue**. The agent console shows case status by polling `GET /cases/{id}`.
- Restart test: kill `app` while a case is pending, restart, approve: the case graph resumes from the Postgres checkpoint.

- **Supervisor (LLM)**: one structured-output call returning `{intent (BANKING77 label), route: rag|tools|action|async|refuse, risk_level, needs_approval, proposed_action?, rationale}`. Routing is scored as in §5.9. `needs_approval` is *not* decided by the LLM alone: it is recomputed deterministically by the approval-matrix rules (§5.2) and the stricter result wins.
- **Retrieval (LLM for rewrite only)**: rewrite query → PGVector similarity with metadata filters (`department`, `document_type`, `sensitivity <= user clearance`) → top-k (k≈5) with scores. Returns `chunks[]{chunk_id, source_path, score, metadata}`.
- **Memory (code)**: loads/saves session summary, entities (account id, ticket id, amounts), case record. Summary refresh is a cheap LLM call run *after* the response streams, off the hot path **[call]**.
- **Tool Execution (code)**: Pydantic-validated args, retry with backoff on transient errors, always returns `{ok, data, error}`; failures are surfaced, never swallowed.
- **Compliance (code-first)**: input: schema/length checks, PII detection and masking (regex for PAN/IBAN/phone/email; Presidio optional **[call]**), prompt-injection heuristics applied to user input *and* retrieved chunks; LLM judge only on borderline scores. Output: citation check (each cited chunk_id was actually retrieved), no unmasked PII, no promises of refunds/outcomes unless an approval exists. Verdict `allow | redact | block` with reason, logged.
- **Approval (code)**: LangGraph `interrupt()` + `approvals` row (`pending`). Runs in the case graph (§5.1). Resume via `POST /approvals/{id}` with `Command(resume=...)` on the case's `thread_id`. Idempotent: a second decision on a decided approval returns 409 with the existing decision and has no side effect.
- **Composer (LLM)**: grounded answer for the agent, the customer-facing script line, citations bound to chunk_ids, recommended action; explicit "I couldn't find policy for this" when retrieval is empty or below threshold.
- **Step / cost budget**: max node visits per run and max token spend per run; exceeding either ends in a safe response and a trace event.

### 5.2 Approval policy (confirmed + accepted refinement)

- **All refunds and credits require supervisor approval** (Tapas). The copilot *drafts* actions and submits only after approval.
- **Five actions, no more** (Tapas):

| Action | Approval | Notes |
|---|---|---|
| Refund / shadow credit | **always** | Tier by ₹ amount (below) |
| Fee reversal | always | Tier by ₹ amount |
| Card chargeback initiation | always | Requires dispute evidence on the case |
| Card block | **no** | Immediate: delay causes harm |
| Escalate complaint (to grievance officer; Ombudsman-ready pack) | no | Required when TAT is breached or customer is unsatisfied |

- The **approval matrix** (synthetic doc, also loaded as structured rules) decides the rest **[call on the figures]**:
  - *who* approves: team lead up to **₹25,000**, operations manager up to **₹2,00,000**, regional operations head above that;
  - extra scrutiny: a second dispute on the same account within 90 days goes one tier up;
  - **refused** outright: anything bypassing identity verification, changing contact details from an unverified request, promising a resolution date the calculator didn't produce.
  - Read-only lookups and ticket notes need no approval.
- **When an action is drafted (Tapas: option a):** the copilot proposes it on its own whenever the intent, the data and the calculator support it. The agent sees the draft and clicks **"Send to supervisor"**; only then are the case and its case graph created. If the agent doesn't click, nothing is filed.
- **Amounts always come from the transaction lookup, never from the customer's words.** If no matching transaction exists, the copilot does not draft and says why.
- **Every pending case carries an RBI-derived due date** (from the calculator) shown in the supervisor queue, sorted by time-to-breach.
- Matrix rules live in a versioned YAML/JSON file derived from the matrix document; the same document is in the RAG corpus so the Composer can cite it.

### 5.3 Tools (≥5; all backed by seeded tables in the same Postgres) **[call]**

| Tool | Kind | Approval |
|---|---|---|
| `ticket_lookup(ticket_id \| account_id)` | read | no |
| `transaction_lookup(account_id, date_range)` | read | no |
| `policy_lookup(policy_id)` (exact fetch by id, current version only) | read | no |
| `org_lookup(role \| team)` (find approver/escalation contact) | read | no |
| `liability_tat_calculator(txn, reported_at, channel, account_type)` → `{customer_liability_inr, shadow_credit_due, resolution_due, compensation_inr_per_day, rule_ids[]}` | pure function | no |
| `add_ticket_note(ticket_id, text)` | write | no |
| `block_card(card_id, reason)` | write | no (safety) |
| `submit_credit(ticket_id, amount_inr, kind: refund\|shadow_credit, reason)` | write | **yes** |
| `submit_fee_reversal(ticket_id, fee_id, amount_inr)` | write | yes |
| `initiate_chargeback(txn_id, reason_code, evidence_ids[])` | write | yes |
| `escalate_complaint(case_id, level: grievance\|ombudsman_pack)` | write | no |
| `draft_customer_notification(ticket_id, template)` | write (draft) | no |

The **calculator** is deterministic code driven by the same rule file as the policies (working-day calendar with Indian bank holidays, reporting-delay bands, per-day compensation). It returns the `rule_ids` it applied so the Composer cites both the policy chunk and the rule. Unit-tested against a table of cases taken from the policy text.

Mock systems are tables (`mock_tickets`, `mock_transactions`, `mock_cards`, `org_directory`) seeded by the data script, so "live tool calls" are real DB I/O with real failure modes (not found, invalid state). One tool should be wrapped with configurable fault injection to demo retries **[call]**.

### 5.4 Data and ingestion

- **BANKING77** (`PolyAI/banking77`): normalized to `query_id, user_query, intent_label, priority, department, risk_level`. Used as (a) the router's labelled eval set, (b) sample customer utterances for the demo, (c) seed text for mock tickets. A fixed subset of intents (refund/dispute/card/transfer families, ~15–20 of 77) is "in scope"; the rest route to `refuse/redirect` **[call]**.
- **Synthetic corpus** (≥25 docs, Markdown with front-matter incl. `effective_from`, `supersedes`, `status: current|superseded`), all for Kestrel Bank, in ₹, RBI-aligned:
  - *Core policies:* customer liability in unauthorised electronic transactions (zero / limited / bank-board-policy bands by reporting delay; shadow credit within 10 working days; resolution within 90 days); failed-transaction TAT and compensation (₹100/day where applicable, per channel: card, ATM, UPI, IMPS); refund & duplicate-charge policy; fee schedule and fee-reversal policy; card chargeback SOP; complaint escalation and grievance redressal, including when a customer can approach the RBI Integrated Ombudsman.
  - *Operations:* approval matrix (₹ tiers), card-block SOP, identity-verification SOP, PII handling guideline (PAN, Aadhaar, card numbers), call scripts, org directory, holiday calendar.
  - *Deliberate traps:* one **superseded** version of a policy with different figures (retrieval must prefer the current one), and one or two docs with an embedded prompt-injection line.
- **Enron**: small side role only: the payload for the **async batch compliance scan** (PII and policy-breach flags). **[call]** The repo ships a curated ~2k-email sample (compressed JSONL, a few MB) produced once by a script from the public archive, so the reviewer never downloads the 1.7 GB tarball. Not part of the RAG corpus for tier-1 answers.
- Pipeline (repeatable script): load → clean/dedupe → chunk → attach metadata (`document_type, department, sensitivity, source_path`) → embed → store. Idempotent re-runs (upsert by stable source path + chunk index).

### 5.5 Schema

Brief's five tables (`documents`, `document_chunks`, `sessions`, `cases`, `approvals`) + `jobs` + LangGraph checkpoint tables + mock-system tables (§5.3). `approvals` gains `approver_role` and `action_payload jsonb`. HNSW index on `embedding`.

### 5.6 API and UI

- `POST /chat` (returns `thread_id`), `GET /stream/{thread_id}` (SSE: node-level progress events + token stream), `POST /drafts/{id}/submit` (agent's "Send to supervisor"; creates the case and starts its case graph), `GET /cases/{id}`, `GET /approvals?state=pending`, `POST /approvals/{id}` (`approve|reject`, reason), `POST /jobs`, `GET /jobs/{id}`, `GET /health` (DB + model reachability).
- Role via header (`X-Role: agent|supervisor|compliance`) **[call]**.
- UI: one static HTML/JS page served by FastAPI with two tabs: **Agent console** (input, live progress, answer with citations, script line) and **Supervisor queue** (pending approvals, evidence, approve/reject) **[call]**. Minimal styling.

### 5.7 Async

Postgres-backed job queue (`jobs` table, `SELECT … FOR UPDATE SKIP LOCKED`) with a separate `worker` container, avoiding Redis **[call]**. Records attempts, retries with cap, writes structured result. Jobs:
- **Enron batch compliance scan** (compliance officer): PII and policy-breach flags over the email sample. `POST /jobs` returns `job_id` immediately.
- **TAT deadline sweeper** (scheduled, every few minutes): finds pending cases close to their RBI-derived due date, marks them `at_risk` / `breached`, bumps them in the supervisor queue and drafts an escalation. No new persona; it makes the async workflow part of the main story.

### 5.8 Observability

Langfuse, with one trace per request and a span per node (latency, tokens, cost, tool args/results, retrieval query/filters/chunk ids/scores, compliance verdicts, failures). **[call, revised after builder review]: self-hosted Langfuse is the default in `docker-compose.yml`**, so a reviewer with only an OpenAI key can open a trace. Cloud keys stay a documented alternative. Fallback that never depends on Langfuse: every node also writes a row to a `run_events` table (node, latency, tokens, cost, verdicts), shown in the UI's case detail. Trace ids are written to `cases`/`approvals` so the supervisor UI can link to the trace.

### 5.9 Evaluation

- Unit tests: PII masking, injection heuristics, matrix rules, tool validation, approval idempotency.
- Integration tests: each of the 5 workflows end to end; approval survives a process restart; duplicate decision.
- ≥20 scenarios across the brief's 5 dimensions, including: superseded-policy trap (must cite the current version), calculator boundary cases (reported on day 3 vs day 4; holiday in between), refund promised without approval (must be blocked by output compliance), injection inside a retrieved doc, duplicate approval decision, restart mid-approval.
- **Eval harness:** one command (`make eval`) runs the scenario set and routing sets and writes a Markdown/JSON report; runnable in CI on every change **[call]**.
- **Model comparison:** the Supervisor and Composer run on two OpenAI models (a small and a larger one); the report has a table of routing accuracy, groundedness, p50/p95 latency and cost per 100 requests (in ₹), with a recommendation **[call on which models]**.
- **Retrieval ablation:** vector-only vs. hybrid (PGVector + Postgres full-text, rank fusion) vs. hybrid + metadata filters, scored by recall@k and MRR on a hand-labelled question→chunk set (~40 questions).
- **Blameless postmortem:** the report includes one real failure found during evaluation, written as an incident (timeline rebuilt from the trace, root cause, fix, regression test). This is the project's nod to incident-response readers.
- Routing, scored two ways **[call]**: (1) **intent accuracy** on a held-out BANKING77 sample, over the ~15–20 in-scope intents with all others collapsed to `out_of_scope` (supervisor prompt carries only the in-scope list); (2) **route accuracy** on a hand-labelled set of ~40 agent requests (rag / tools / action / async / refuse), because BANKING77 has no route labels and the same intent can need different routes. Groundedness via an LLM judge with a rubric, spot-checked by hand **[call]**. Report includes failures and analysis.

### 5.10 Packaging

Compose services: `app` (FastAPI), `worker`, `postgres` (pgvector image), self-hosted Langfuse services (on by default, see §5.8). A one-shot `ingest` service runs automatically on `compose up` (idempotent; skips if already loaded); it needs only `OPENAI_API_KEY` and internet for the Hugging Face download. Synthetic docs and the Enron sample are committed to the repo **[call]**. `.env.example`, README with start + readiness verification.

### 5.11 Positioning and metrics (for ops/compliance readers)

The README and write-up lead with the business problem and report, next to the AI metrics:
- **Estimated handling time saved per call** (copilot latency vs. a timed manual policy lookup on the same questions; method stated honestly).
- **Unapproved-promise rate:** share of responses that commit to a refund/credit/date without an approval or calculator output. Target **0**, enforced by output compliance.
- **TAT breach rate** on pending cases in a simulated week (sweeper on), and median time-to-decision.
- **Audit completeness:** share of actions with a full trace, an approval record and cited rule ids.
- **Groundedness and citation accuracy.**

## 6. Left to the builder

Prompt wording; exact chunk sizes within the brief's range; k and score threshold tuning; injection heuristic details; library choice for PII (regex vs Presidio); the exact in-scope BANKING77 intent list; UI styling; test framework layout; specific synthetic doc contents (consistent with §5.2–5.4); the exact two models compared; the Indian holiday list used by the calculator.
