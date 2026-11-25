# Architecture

Kestrel Resolution Copilot is two LangGraph graphs on one Postgres, behind a FastAPI app,
with a worker for background jobs and self-hosted Langfuse for traces.

```mermaid
flowchart LR
  subgraph UI["Browser (kestrel/ui)"]
    A[Agent console]:::ui
    S[Supervisor queue]:::ui
  end
  A -- "POST /chat (note, idv_level)\nGET /stream (SSE)" --> API
  A -- "POST /drafts/{id}/submit" --> API
  S -- "GET /approvals, POST /approvals/{id}" --> API

  subgraph API["FastAPI app"]
    CG[Chat graph\nnever pauses]
    KG[Case graph\npauses at approval]
  end

  CG -- draft --> DB[(Postgres + pgvector\ncorpus, mock bank, drafts,\ncases, approvals, run_events,\nLangGraph checkpoints, jobs)]
  KG -- "interrupt() / resume\n(checkpoint survives restart)" --> DB
  W[Worker\nEnron scan, TAT sweeper,\nmemory summary] --> DB
  CG -. spans .-> LF[Langfuse v3\n+ ClickHouse, Redis, S3]
  KG -. spans .-> LF
  CG -- "3 LLM calls" --> OAI[OpenAI\ngpt-5.4-mini / nano\ntext-embedding-3-small]
  classDef ui fill:#eef4ff,stroke:#0b5cad
```

## Chat graph (one run per agent message)

```mermaid
flowchart TD
  IN[compliance_in\nPII already masked at the API;\ninjection heuristics + LLM judge for borderline] -->|block| BL[blocked]
  IN --> ML[memory_load\nentities + background summary]
  ML --> SUP{{supervisor\nLLM: intent, route, risk}}
  SUP -->|refuse / matrix refusal\nKB-REF-1/2 in code| CO
  SUP -->|rag| RET
  SUP -->|tools / action| TE[tool_exec\ntransaction lookup, card block if IDV ≥ 1,\nliability/TAT calculator]
  TE --> RET[[retrieval\nLLM rewrite → vector search,\nstatus + txn-date filters,\nline-level quarantine of trap text]]
  RET -->|action| DA[draft_action\ncode: action, amount from lookup,\napprover tier, IDV ≥ 2 + KYC]
  RET -->|rag/tools| CO
  DA --> CO{{composer\nLLM: cited answer, allowed chunk ids listed}}
  CO --> OUT[compliance_out\ncitation check, promise block,\nPII, numbers must appear in facts]
  OUT --> MS[memory_save]
```

Double boxes call an LLM; everything that touches money, dates or permissions is code.

## Case graph (one per submitted draft)

`submit` → `request_approval` (writes the approval row with ₹ tier and RBI due date, then
`interrupt()`) → *process may die here; the checkpoint is in Postgres* → supervisor decides
via `POST /approvals/{id}` (server checks the approver's tier; a second decision gets 409) →
`execute_action` (posts the credit once: unique index per transaction) → `notify` (SMS draft).
`reconcile()` at boot resumes any case whose approval was decided while the app was down.

## Why only three LLM calls ("why some agents are code")

The brief names seven agent roles. Four of them (compliance, approval, memory, tool execution)
are deterministic code here, because each one either moves money, computes a regulatory
date, or enforces a permission, and in each of those a wrong answer is a regulatory breach,
not a bad sentence. Code is testable with a table of cases (`tests/unit/test_calculator.py`),
gives the same answer every time, and costs nothing per call. The LLM is used where language
is the problem: classifying a messy agent note (Supervisor), turning it into a search query
(rewrite), and writing a short cited answer (Composer).

The evaluation supports this only partly, and the limit matters. No failing scenario had a
wrong *calculation*: given the inputs, the amount, the due date and the approver tier were
always right. But the inputs are not all code. The Supervisor's intent and its extracted flags
decide *which* calculation runs, and in eval run 1 that steered money twice: an LLM-set
"customer at fault" flag with no evidence in the note (S24) changed the liability outcome, and
an ATM cash-not-dispensed note classified as fraud (S09) would have blocked a card and applied
the fraud-liability rule to a failed debit. Both are now overridden in code (the fault flag
needs matching text in the note; a `failed_debited` transaction is never treated as fraud,
D42, D43). The honest claim is narrower: code computes every number, but an LLM still chooses
between code paths, and each such choice needs its own deterministic cross-check against bank
data. See `reports/eval.md` for the remaining routing failures (S08b, S09 intent label).

The trade-off: the deterministic graph handles only the intents it was built for. A new
dispute type needs code, not a prompt change. For a regulated desk that is the right default.
