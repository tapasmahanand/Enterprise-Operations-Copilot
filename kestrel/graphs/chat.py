"""Chat graph: one run per agent message, never paused (thread_id = session id).

    compliance_in ─block─► blocked ─► END
         │
    memory_load ─► supervisor ─┬─ refuse ──────────────────────────────┐
                               ├─ rag ─────────────► retrieval ────────┤
                               └─ tools / action ─► tool_exec ─► retrieval ─► draft_action ─┤
                                                                       composer ◄───────────┘
                                                                          ▼
                                                                    compliance_out ─► memory_save ─► END

Tool execution runs *before* retrieval on the tools/action routes, because the policy
version depends on the transaction date (D13): we must know which transaction it is
before we know which liability regime's documents are in scope.

Only three nodes call an LLM: supervisor, the query rewrite inside retrieval, composer.
Everything with money or dates in it is code (D5).
"""

from __future__ import annotations

import os
import re
from datetime import date
from typing import Any, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from psycopg.types.json import Jsonb

from kestrel.cases import create_draft
from kestrel.checkpoint import get_checkpointer
import yaml

from kestrel.config import ROOT, get_settings
from kestrel.db import connect
from kestrel.guards.injection import injection_score
from kestrel.guards.output import check_output
from kestrel.guards.pii import mask_pii
from kestrel.ingest.embed import make_embedder
from kestrel.ingest.seed import anchor_date
from kestrel.llm import SupervisorDecision, Usage, get_llm, model_for
from kestrel.retrieval.search import vector_search
from kestrel.rules.approval import approval_requirement
from kestrel.rules.loader import load_rules
from kestrel.tools.bank import block_card, liability_calculator, transaction_lookup
from kestrel.tracing import traced

FRAUD_INTENTS = {"card_payment_not_recognised", "compromised_card", "lost_or_stolen_card",
                 "cash_withdrawal_not_recognised", "direct_debit_payment_not_recognised"}
MAX_TOKENS_PER_RUN = 20_000


class ChatState(TypedDict, total=False):
    run_id: str
    thread_id: str
    session_id: str
    account_id: str | None          # customer context from the agent's CRM screen, if any
    masked: str                     # PII-masked agent note; the raw text is never in state
    pii: dict[str, Any]
    blocked_reason: str | None
    entities: dict[str, Any]
    sup: dict[str, Any]
    tool_calls: list[dict[str, Any]]
    txn: dict[str, Any] | None
    card_block: dict[str, Any] | None
    calc: dict[str, Any] | None
    hits: list[dict[str, Any]]
    query: str
    draft: dict[str, Any] | None
    no_draft_reason: str | None
    answer: str
    citations: list[str]
    script: str | None
    output_check: dict[str, Any]
    tokens_used: int


def progress(node: str, label: str, **extra: Any) -> None:
    try:
        get_stream_writer()({"type": "progress", "node": node, "label": label, **extra})
    except Exception:  # noqa: BLE001 - outside a streaming run (e.g. unit tests)
        pass


def _emit(kind: str, **payload: Any) -> None:
    try:
        get_stream_writer()({"type": kind, **payload})
    except Exception:  # noqa: BLE001
        pass


# --- nodes ----------------------------------------------------------------------

@traced("compliance_in")
def compliance_in(state: ChatState) -> dict:
    """PII was masked at the API boundary (run_chat) so the raw text never reaches a
    checkpoint. This node records that verdict and runs the injection heuristics."""
    progress("compliance_in", "checking input")
    pii = state["pii"]
    score, hits = injection_score(state["masked"])
    out: dict[str, Any] = {"tokens_used": 0}
    if score >= 1.0:
        out["blocked_reason"] = f"possible prompt injection ({', '.join(hits)})"
        verdict = "block"
    else:
        verdict = "redact" if pii["found"] else "allow"
    out["_trace"] = {"status": "blocked" if verdict == "block" else "ok",
                     "detail": {"verdict": verdict, "pii_found": pii["found"], "injection": hits}}
    return out


@traced("blocked")
def blocked(state: ChatState) -> dict:
    ans = ("I can't process this request: " + state["blocked_reason"] +
           ". The attempt has been logged. Please rephrase the customer's issue in your own words.")
    _emit("token", text=ans)
    return {"answer": ans, "citations": [], "script": None}


@traced("memory_load")
def memory_load(state: ChatState) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT entities FROM sessions WHERE session_id = %s", (state["session_id"],)).fetchone()
    ents = row[0] if row else {}
    return {"entities": ents, "_trace": {"detail": {"entities": sorted(ents)}}}


INTENTS: dict[str, dict] = yaml.safe_load((ROOT / "rules" / "intents.yaml").read_text())["in_scope"]


def _intent_list() -> str:
    return "\n".join(f"- {k} (default route: {v['route']})" for k, v in INTENTS.items())


SUPERVISOR_SYSTEM = """You are the Supervisor of a tier-1 resolution copilot at Kestrel Bank (an Indian retail bank).
A bank agent types what the customer on the phone is saying. Classify it and choose a route.

In-scope intents (BANKING77 labels):
{intents}
Anything else: intent=out_of_scope, route=refuse.

Routes:
- rag: a policy/process question that needs only documents.
- tools: needs a lookup in bank systems (transactions, tickets) but no money movement.
- action: the copilot should draft an action (credit, fee reversal, chargeback, card block, escalation).
- async: a batch/background job (compliance scan); only for compliance staff.
- refuse: out of scope, or asks to bypass identity checks, change contact details unverified, or promise outcomes.

Card numbers in the text are already masked to XXXX-1234; report the last 4 digits in card_last4.
Do not compute liability or dates. Reply with the structured fields only."""


@traced("supervisor")
def supervisor(state: ChatState) -> dict:
    progress("supervisor", "classifying")
    ctx = state.get("entities") or {}
    user = state["masked"] + (f"\n\n(Known from earlier in this call: {ctx})" if ctx else "")
    d, usage = get_llm().structured(SUPERVISOR_SYSTEM.format(intents=_intent_list()), user,
                                    SupervisorDecision, model=model_for("supervisor"))
    if d.intent == "out_of_scope":
        d.route = "refuse"
    if not d.card_last4 and state["pii"]["card_last4"]:
        d.card_last4 = state["pii"]["card_last4"][-1]
    if state["pii"].get("secret_shared"):
        d.customer_shared_credentials = True
    return {"sup": d.model_dump(), "tokens_used": state.get("tokens_used", 0) + usage.tokens_in + usage.tokens_out,
            "_trace": {"detail": {"intent": d.intent, "route": d.route, "rationale": d.rationale,
                                  "model": usage.model}, **usage.trace()}}


def _pick_txn(rows: list[dict], amount: int | None) -> tuple[dict | None, str | None]:
    if amount is not None:
        exact = [r for r in rows if r["amount_inr"] == amount]
        if len(exact) == 1:
            return exact[0], None
        if not exact:
            return None, f"no transaction of ₹{amount:,} found on this card/account in the last 30 days"
        return None, f"{len(exact)} transactions of ₹{amount:,}; ask the customer which one"
    if len(rows) == 1:
        return rows[0], None
    return None, "several recent transactions; ask the customer for the amount or date"


@traced("tool_exec")
def tool_exec(state: ChatState) -> dict:
    sup, ents = state["sup"], state.get("entities") or {}
    calls: list[dict] = []
    account = state.get("account_id") or ents.get("account_id")
    last4 = sup.get("card_last4") or ents.get("card_last4")
    as_of = anchor_date()

    progress("tool_exec", "looking up transactions")
    lk = transaction_lookup(account_id=account, card_last4=last4, as_of=as_of)
    calls.append({"tool": "transaction_lookup", "args": {"account_id": account, "card_last4": last4},
                  "ok": lk.ok, "error": lk.error, "n": len(lk.data or [])})
    rows = lk.data or []
    txn, why = (None, lk.error or "no card or account given") if not lk.ok else _pick_txn(rows, sup.get("amount_inr"))
    out: dict[str, Any] = {"tool_calls": calls, "txn": _jsonable(txn), "no_draft_reason": why}

    if sup["intent"] in FRAUD_INTENTS and (txn or rows):
        card_id = (txn or rows[0])["card_id"]
        if card_id:
            progress("tool_exec", "blocking card")
            b = block_card(card_id=card_id, reason=f"customer reports {sup['intent']}")
            calls.append({"tool": "block_card", "args": {"card_id": card_id}, "ok": b.ok, "error": b.error})
            out["card_block"] = b.data if b.ok else {"error": b.error}

    if txn and sup["intent"] in FRAUD_INTENTS:
        progress("tool_exec", "calculating liability")
        fault = "customer" if sup.get("customer_shared_credentials") else "third_party"
        c = liability_calculator(amount_inr=txn["amount_inr"], txn_date=txn["txn_date"], reported_on=as_of,
                                 fault=fault, product=txn["product"], alert_received_on=txn["alert_sent_on"],
                                 credit_limit_inr=txn["credit_limit_inr"], cross_border=txn["cross_border"])
        calls.append({"tool": "liability_calculator", "ok": c.ok, "error": c.error,
                      "args": {"fault": fault, "reported_on": as_of.isoformat()}})
        out["calc"] = {**c.data, "fault_assumed": fault} if c.ok else None
        if not c.ok:
            out["no_draft_reason"] = f"calculator refused: {c.error}"
    out["_trace"] = {"detail": {"calls": calls}, "status": "ok" if all(x["ok"] for x in calls) else "error"}
    return out


REWRITE_SYSTEM = ("Rewrite the bank agent's note as a short search query (max 12 words) for Kestrel Bank's internal "
                  "policy documents. Use formal banking-policy vocabulary for the customer's actual problem only; "
                  "do not add other topics, amounts, names or card digits. Return only the query.")


@traced("retrieval")
def retrieval(state: ChatState) -> dict:
    progress("retrieval", "retrieving policy")
    q, usage = get_llm().text(REWRITE_SYSTEM, state["masked"], model=model_for("rewrite"))
    q = q.strip() or state["masked"]
    txn = state.get("txn")
    txn_date = date.fromisoformat(txn["txn_date"]) if txn else None
    s = get_settings()
    vec = make_embedder(s).embed([q])[0]
    with connect() as conn:
        hits = vector_search(conn, vec, k=10, txn_date=txn_date)
    min_score = float(os.environ.get("RETRIEVAL_MIN_SCORE", "0.2"))
    # Retrieved text is untrusted: a chunk that trips the injection heuristics is
    # quarantined (logged, never shown to the Composer).
    quarantined = [h.chunk_id for h in hits if injection_score(h.text, "document")[0] >= 1.0]
    # Intent → policy boost: intents.yaml maps each in-scope intent to its governing
    # policy; chunks from that policy get +0.1 so the Composer sees the rule that
    # actually applies (D21). The boost and the raw score are both in the trace.
    policy = INTENTS.get(state["sup"].get("intent"), {}).get("policy")
    scored = [(h.score + (0.1 if policy and h.document_id.startswith(policy) else 0.0), h)
              for h in hits if h.chunk_id not in quarantined and h.score >= min_score]
    scored.sort(key=lambda x: -x[0])
    out = [{"chunk_id": h.chunk_id, "document_id": h.document_id, "title": h.title, "source_path": h.source_path,
            "score": round(h.score, 4), "boosted": round(b, 4), "text": h.text} for b, h in scored[:5]]
    kept = out
    _emit("citations_candidates", chunks=[{k: v for k, v in o.items() if k != "text"} for o in out])
    return {"hits": out, "query": q, "tokens_used": state.get("tokens_used", 0) + usage.tokens_in + usage.tokens_out,
            "_trace": {"detail": {"query": q, "txn_date_filter": txn_date.isoformat() if txn_date else None,
                                  "chunks": [(o["chunk_id"], o["score"]) for o in out],
                                  "policy_boost": policy, "quarantined": quarantined,
                                  "dropped_below_threshold": sum(1 for h in hits if h.score < min_score)}, **usage.trace()}}


@traced("draft_action")
def draft_action(state: ChatState) -> dict:
    sup, txn, calc = state["sup"], state.get("txn"), state.get("calc")
    # Whether a credit is drafted is decided here from intent + data, not from the LLM's
    # proposed_action: for a fraud report the LLM tends to say "card_block", but the
    # credit question is answered by the calculator (D20).
    credit_intents = FRAUD_INTENTS | {"transaction_charged_twice"}
    if sup["route"] != "action" or (sup["intent"] not in credit_intents and sup.get("proposed_action") != "credit"):
        return {"draft": None}
    if not txn:
        return {"draft": None, "_trace": {"detail": {"no_draft": state.get("no_draft_reason")}}}
    if sup["intent"] in FRAUD_INTENTS:
        if not calc:
            return {"draft": None}
        if calc["needs_review"] or calc["bank_liability_inr"] <= 0:
            return {"draft": None, "no_draft_reason": "calculator: no bank-funded credit due; case needs review",
                    "_trace": {"detail": {"no_draft": "calculator"}}}
        amount, kind = calc["bank_liability_inr"], "shadow_credit"
    else:
        amount, kind = txn["amount_inr"], "refund"
    progress("draft_action", "drafting shadow credit" if kind == "shadow_credit" else "drafting refund")
    req = approval_requirement("credit", amount, load_rules())
    if req.refused:
        return {"draft": None, "no_draft_reason": f"refused by matrix rule {req.refusal_rule}"}
    action = {"action": "credit", "kind": kind, "amount_inr": amount,  # amount from the lookup, never the customer (D4)
              "account_id": txn["account_id"], "txn_id": txn["txn_id"], "card_last4": txn.get("card_last4"),
              "merchant": txn.get("merchant"), "approver_role": req.approver_role, "risk_level": sup["risk_level"],
              "intent": sup["intent"], "due_by": calc.get("shadow_credit_due") if calc else None,
              "reason": f"{sup['intent']}: {txn['txn_id']} ₹{amount:,}"}
    evidence = [{"chunk_id": h["chunk_id"], "title": h["title"], "score": h["score"]} for h in state.get("hits", [])]
    evidence.append({"tool": "transaction_lookup", "txn": txn})
    if state.get("card_block"):
        evidence.append({"tool": "block_card", "result": state["card_block"]})
    draft_id = create_draft(state["session_id"], state["run_id"], action, evidence, calc)
    draft = {"draft_id": draft_id, **action, "rule_ids": (calc or {}).get("rule_ids", [])}
    _emit("draft", draft=draft)
    return {"draft": draft, "_trace": {"detail": {"draft_id": draft_id, "amount_inr": amount,
                                                  "approver_role": req.approver_role}}}


COMPOSER_SYSTEM = """You are the Composer for Kestrel Bank's tier-1 resolution copilot. Write a short answer (max ~80 words, 2-4 sentences)
for the BANK AGENT (not the customer) on a live call.

Rules:
- Use only the FACTS and the POLICY CHUNKS below. Cite every policy statement with its chunk id in square brackets,
  exactly as given, e.g. [KB-POL-LIAB-A:0]. Mention rule ids from FACTS (e.g. R17-6ii) where they apply.
- Every amount and date you state must appear in FACTS. Never compute or estimate amounts or dates yourself.
- Never promise a refund, credit or outcome as final: credits need supervisor approval. If a draft exists, tell the
  agent to review it and click "Send to supervisor".
- If the chunks don't cover the question, say "I couldn't find policy for this" and stop.
- Lead with what the agent should do or say next. Don't restate every fact: the UI already shows the
  calculator figures, the draft and the script.
- Plain text, no headings. Card numbers only as last 4 digits."""


def _facts(state: ChatState) -> str:
    lines = []
    sup = state.get("sup") or {}
    lines.append(f"intent: {sup.get('intent')} (route {sup.get('route')})")
    if t := state.get("txn"):
        lines.append(f"transaction: {t['txn_id']} on {t['txn_date']}, ₹{t['amount_inr']:,} at {t['merchant']}, "
                     f"channel {t['channel']}, card ending {t.get('card_last4')}")
    if cb := state.get("card_block"):
        lines.append(f"card block: {'card ending ' + str(cb.get('last4')) + ' is now BLOCKED' if 'error' not in cb else 'FAILED ' + cb['error']}")
    if c := state.get("calc"):
        lines.append(f"liability calculator ({c['regime']} regime, fault assumed: {c['fault_assumed']}): customer liability "
                     f"₹{c['customer_liability_inr']:,}; bank liability ₹{c['bank_liability_inr']:,}; shadow credit due by "
                     f"{c['shadow_credit_due']}; resolution due by {c['resolution_due']}; rule ids {', '.join(c['rule_ids'])}; "
                     f"{c['explanation']}")
    if d := state.get("draft"):
        lines.append(f"draft {d['draft_id']}: {d['kind']} of ₹{d['amount_inr']:,}, needs approval by {d['approver_role']} "
                     f"(KB-MATRIX); not yet sent to supervisor")
    elif r := state.get("no_draft_reason"):
        lines.append(f"no action drafted: {r}")
    if (sup.get("customer_shared_credentials") is None) and sup.get("intent") in FRAUD_INTENTS:
        lines.append("not yet confirmed with customer: whether they shared OTP/PIN/card details (affects liability)")
    return "\n".join(f"- {x}" for x in lines)


@traced("composer")
def composer(state: ChatState) -> dict:
    progress("composer", "writing answer")
    if state["sup"]["route"] == "refuse":
        ans = ("This request is outside what the copilot handles at tier 1 (classified as "
               f"{state['sup']['intent']}). Please use the standard servicing process or escalate to your team lead.")
        _emit("token", text=ans)
        return {"answer": ans, "citations": []}
    if state.get("tokens_used", 0) > MAX_TOKENS_PER_RUN:
        ans = "This request hit the copilot's token budget for one run. Please narrow the question."
        _emit("token", text=ans)
        return {"answer": ans, "citations": [], "_trace": {"status": "blocked", "detail": {"budget": "tokens"}}}
    chunks = "\n\n".join(f"[chunk_id={h['chunk_id']}] {h['title']}\n{h['text']}" for h in state.get("hits", []))
    user = f"AGENT NOTE (masked): {state['masked']}\n\nFACTS:\n{_facts(state)}\n\nPOLICY CHUNKS:\n{chunks or '(none)'}"
    parts: list[str] = []
    usage = Usage(model_for("composer"))
    for piece in get_llm().stream(COMPOSER_SYSTEM, user, model=model_for("composer")):
        if isinstance(piece, Usage):
            usage = piece
        else:
            parts.append(piece)
            _emit("token", text=piece)
    ans = "".join(parts).strip()
    return {"answer": ans, "tokens_used": state.get("tokens_used", 0) + usage.tokens_in + usage.tokens_out,
            "_trace": {"detail": {"model": usage.model, "chars": len(ans)}, **usage.trace()}}


def _script(state: ChatState) -> str | None:
    """Customer-facing script. Deterministic: every ₹ and date comes from tools (KB-SCRIPT-FRAUD)."""
    c, d, cb = state.get("calc"), state.get("draft"), state.get("card_block") or {}
    if not d:
        return None
    lines = []
    if cb.get("status") == "blocked":
        lines.append(f"I've blocked your card ending {cb['last4']} so no further payments can go through.")
    if c and c["customer_liability_inr"] == 0:
        lines.append(f"Because you reported this within the allowed time, you won't be liable for this "
                     f"₹{d['amount_inr']:,} payment. We'll credit it to your account by "
                     f"{date.fromisoformat(c['shadow_credit_due']).strftime('%d %B %Y')} while we investigate.")
    lines.append("I'm sending this to my supervisor for approval. You'll get an SMS confirming it within 24 hours.")
    return " ".join(lines)


@traced("compliance_out")
def compliance_out(state: ChatState) -> dict:
    retrieved = {h["chunk_id"] for h in state.get("hits", [])}
    res = check_output(state.get("answer", ""), retrieved, approval_exists=False,
                       allowed_numbers=_allowed_numbers(state))
    ans = state.get("answer", "")
    if res["verdict"] == "block":
        ans = ("[The drafted answer was withheld by the output check: " + "; ".join(res["violations"]) +
               ".]\n\nFacts from bank systems:\n" + _facts(state))
        _emit("replace_answer", text=ans)
    script = _script(state)
    if (d := state.get("draft")) and res["verdict"] == "allow":
        # The supervisor sees the chunks the answer actually cited, plus the tool evidence.
        cited = [{"chunk_id": h["chunk_id"], "title": h["title"], "score": h["score"]}
                 for h in state.get("hits", []) if h["chunk_id"] in res["citations"]]
        tools = [{"tool": "transaction_lookup", "txn": state.get("txn")}]
        if state.get("card_block"):
            tools.append({"tool": "block_card", "result": state["card_block"]})
        with connect() as conn:
            conn.execute("UPDATE drafts SET evidence = %s WHERE draft_id = %s AND case_id IS NULL",
                         (Jsonb(cited + tools), d["draft_id"]))
            conn.commit()
    out = {"answer": ans, "citations": res["citations"], "script": script, "output_check": res,
           "_trace": {"status": "blocked" if res["verdict"] == "block" else "ok", "detail": res}}
    _emit("final", answer=ans, citations=res["citations"], script=script, draft=state.get("draft"),
          output_check=res, card_block=state.get("card_block"), calc=state.get("calc"))
    return out


def _allowed_numbers(state: ChatState) -> set[str]:
    """Every number the answer may state: those in FACTS, the masked note and the retrieved chunks."""
    blob = _facts(state) + state.get("masked", "") + " ".join(h["text"] for h in state.get("hits", []))
    return set(re.findall(r"\d[\d,]*", blob))


@traced("memory_save")
def memory_save(state: ChatState) -> dict:
    ents = dict(state.get("entities") or {})
    if t := state.get("txn"):
        ents.update({"account_id": t["account_id"], "card_last4": t.get("card_last4"), "txn_id": t["txn_id"]})
    if d := state.get("draft"):
        ents["draft_id"] = d["draft_id"]
    with connect() as conn:
        conn.execute(
            """INSERT INTO sessions (session_id, user_id, entities, last_route) VALUES (%s, 'agent', %s, %s)
               ON CONFLICT (session_id) DO UPDATE SET entities = EXCLUDED.entities, last_route = EXCLUDED.last_route,
               updated_at = now()""",
            (state["session_id"], Jsonb(ents), (state.get("sup") or {}).get("route")))
        conn.commit()
    return {"entities": ents}


def _jsonable(x: Any) -> Any:
    if x is None:
        return None
    return {k: (v.isoformat() if isinstance(v, date) else v) for k, v in x.items()}


# --- graph ----------------------------------------------------------------------

def _after_compliance(state: ChatState) -> str:
    return "blocked" if state.get("blocked_reason") else "memory_load"


def _after_supervisor(state: ChatState) -> str:
    r = state["sup"]["route"]
    return {"rag": "retrieval", "tools": "tool_exec", "action": "tool_exec"}.get(r, "composer")


def _after_retrieval(state: ChatState) -> str:
    return "draft_action" if state["sup"]["route"] == "action" else "composer"


def build_chat_graph():
    g = StateGraph(ChatState)
    for name, fn in [("compliance_in", compliance_in), ("blocked", blocked), ("memory_load", memory_load),
                     ("supervisor", supervisor), ("tool_exec", tool_exec), ("retrieval", retrieval),
                     ("draft_action", draft_action), ("composer", composer), ("compliance_out", compliance_out),
                     ("memory_save", memory_save)]:
        g.add_node(name, fn)
    g.add_edge(START, "compliance_in")
    g.add_conditional_edges("compliance_in", _after_compliance, ["blocked", "memory_load"])
    g.add_edge("blocked", END)
    g.add_edge("memory_load", "supervisor")
    g.add_conditional_edges("supervisor", _after_supervisor, ["retrieval", "tool_exec", "composer"])
    g.add_edge("tool_exec", "retrieval")
    g.add_conditional_edges("retrieval", _after_retrieval, ["draft_action", "composer"])
    g.add_edge("draft_action", "composer")
    g.add_edge("composer", "compliance_out")
    g.add_edge("compliance_out", "memory_save")
    g.add_edge("memory_save", END)
    return g.compile(checkpointer=get_checkpointer())


_graph = None


def chat_graph():
    global _graph
    if _graph is None:
        _graph = build_chat_graph()
    return _graph


# Fields that belong to one run. The session thread keeps its checkpoint across
# messages, so each run starts by clearing these explicitly.
_PER_RUN_RESET = {"blocked_reason": None, "sup": {}, "tool_calls": [], "txn": None, "card_block": None,
                  "calc": None, "hits": [], "query": "", "draft": None, "no_draft_reason": None, "answer": "",
                  "citations": [], "script": None, "output_check": {}, "tokens_used": 0}


def run_input(run_id: str, session_id: str, message: str, account_id: str | None = None) -> dict:
    m = mask_pii(message)
    return {**_PER_RUN_RESET, "run_id": run_id, "thread_id": session_id, "session_id": session_id,
            "account_id": account_id, "masked": m.text,
            "pii": {"found": m.found, "card_last4": m.card_last4, "secret_shared": m.secret_shared}}


def stream_run(run_id: str, session_id: str, message: str, account_id: str | None = None):
    """Yields progress/token/draft/final events as dicts."""
    cfg = {"configurable": {"thread_id": session_id}, "recursion_limit": 20}  # node-visit budget
    for ev in chat_graph().stream(run_input(run_id, session_id, message, account_id), cfg, stream_mode="custom"):
        yield ev
