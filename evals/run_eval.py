"""`make eval`: end-to-end scenarios, routing accuracy, groundedness judge, ops/compliance
metrics, model comparison and the retrieval ablation -> reports/eval.md (+ eval.json).

Runs in its own database (`kestrel_eval`, re-created each time) with the demo data seeded
at a fixed anchor date, so results don't depend on the day it runs or on the demo DB.
Needs Postgres and OPENAI_API_KEY (real models; there is no point evaluating the fake).

    python -m evals.run_eval                      # production config + gpt-5.4 comparison
    python -m evals.run_eval --configs prod       # one config only (faster, cheaper)
    python -m evals.run_eval --routing-n 0        # skip the BANKING77 routing sample
"""

from __future__ import annotations

from kestrel.access import TIER1

import argparse
import csv
import json
import os
import random
import re
import statistics
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import psycopg
import yaml
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
ANCHOR = date(2026, 11, 24)
JUDGE_MODEL = "gpt-5.4"
CONFIGS = {
    "prod": {"label": "gpt-5.4-mini (production)", "MODEL_PROFILE": "production",
             "SUPERVISOR_MODEL": "gpt-5.4-mini", "COMPOSER_MODEL": "gpt-5.4-mini"},
    "big": {"label": "gpt-5.4 (demo profile)", "MODEL_PROFILE": "demo", "SUPERVISOR_MODEL": "gpt-5.4", "COMPOSER_MODEL": "gpt-5.4"},
}
NODES_FOR_AUDIT = ["compliance_in", "memory_load", "supervisor", "tool_exec", "retrieval", "draft_action",
                   "composer", "compliance_out", "memory_save"]


# --- setup ----------------------------------------------------------------------------

def setup_db() -> str:
    admin = os.environ.get("DATABASE_URL", "postgresql://kestrel:kestrel@localhost:5432/kestrel")
    base = admin.rpartition("/")[0]
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS kestrel_eval WITH (FORCE)")
        c.execute("CREATE DATABASE kestrel_eval")
    url = f"{base}/kestrel_eval"
    os.environ.update({"DATABASE_URL": url, "SEED_ANCHOR_DATE": ANCHOR.isoformat(), "LLM_PROVIDER": "openai",
                       "EMBEDDING_PROVIDER": os.environ.get("EMBEDDING_PROVIDER", "openai")})
    os.environ.pop("LANGFUSE_PUBLIC_KEY", None)   # eval runs are recorded in run_events only
    from kestrel.config import get_settings
    from kestrel.db import apply_schema, connect
    from kestrel.ingest.embed import make_embedder
    from kestrel.ingest.run import ingest_corpus
    from kestrel.ingest.seed import seed
    with connect() as conn:
        apply_schema(conn)
        seed(conn, anchor=ANCHOR, force=True)
        ingest_corpus(conn, get_settings(), make_embedder(get_settings()))
    return url


# --- scenarios --------------------------------------------------------------------------

class Claim(BaseModel):
    claim: str
    supported: bool


class Groundedness(BaseModel):
    claims: list[Claim] = Field(description="Every factual claim in the ANSWER, each judged against FACTS and SOURCES")


JUDGE_SYSTEM = """You audit answers written by a bank copilot for a call-centre agent.
List every factual claim in the ANSWER (amounts, dates, deadlines, rules, who approves what, what the agent should do
because a policy says so). Do not list advice that is only a caution or a conditional ("if X, do not Y") unless
it asserts a fact. For each, say whether it is directly supported by the AGENT NOTE, the FACTS or the SOURCES. Paraphrase is fine;
anything not stated there is unsupported, even if it is true in the real world. Instructions to the agent that merely
restate the workflow ("review the draft", "send to supervisor") are supported only if FACTS mention a draft; telling
the agent to send a draft that FACTS say does not exist is unsupported."""

def _doc(chunk_id: str) -> str:
    return chunk_id.rsplit(":", 1)[0]


def run_scenario(sc: dict, judge: bool) -> dict:
    from kestrel.db import connect
    from kestrel.graphs.chat import _facts, chat_graph, stream_run
    from kestrel.guards.output import _CITE, _unnegated_promise, premature_submission
    from kestrel.llm import get_llm
    with connect() as conn:   # every scenario starts from the same bank state
        conn.execute("UPDATE mock_cards SET status = 'active'")
        conn.execute("DELETE FROM drafts WHERE case_id IS NULL")
        conn.commit()
    run_id, session = f"EVAL-{uuid.uuid4().hex[:8]}", f"S-EVAL-{uuid.uuid4().hex[:6]}"
    t0 = time.perf_counter()
    first_token = None
    raw, final, events = [], None, []
    for ev in stream_run(run_id, session, sc["note"], sc.get("account"), sc.get("idv", 0), principal=TIER1):
        events.append(ev)
        if ev["type"] == "token":
            first_token = first_token or time.perf_counter() - t0
            raw.append(ev["text"])
        elif ev["type"] == "final":
            final = ev
    latency = time.perf_counter() - t0
    st = chat_graph().get_state({"configurable": {"thread_id": session}}).values
    with connect() as conn:
        rows = conn.execute("SELECT node, status, tokens_in, tokens_out, cost_inr FROM run_events WHERE run_id = %s",
                            (run_id,)).fetchall()
        draft_row = conn.execute("SELECT action, evidence FROM drafts WHERE run_id = %s", (run_id,)).fetchone() \
            if final and final.get("draft") else None
    raw_text = "".join(raw)
    ans = (final or {}).get("answer") or raw_text
    sup = st.get("sup") or {}
    retrieved = {h["chunk_id"] for h in st.get("hits", [])}
    exp = sc.get("expect", {})
    checks: dict[str, bool] = {}
    if "intent" in exp:
        checks["intent"] = sup.get("intent") in exp["intent"]
    if "route" in exp:
        checks["route"] = sup.get("route") in exp["route"]
    blocked = final is None
    if exp.get("blocked"):
        checks["blocked"] = blocked
    d = (final or {}).get("draft")
    if "draft" in exp:
        want = exp["draft"]
        if want is None:
            checks["draft"] = d is None
        else:
            checks["draft"] = bool(d) and d["kind"] == want["kind"] and d["amount_inr"] == want["amount"] and \
                ("approver" not in want or d["approver_role"] == want["approver"])
    if "refusal" in exp:
        checks["refusal"] = ((final or {}).get("refusal") or {}).get("rule_id") == exp["refusal"]
    if "card_block" in exp:
        checks["card_block"] = (((final or {}).get("card_block") or {}).get("status") == "blocked") == exp["card_block"]
    cited = (final or {}).get("citations") or []
    cited_docs = {_doc(c) for c in cited}
    if "cite" in exp:
        checks["cite"] = bool(cited_docs & set(exp["cite"]))
    if "no_cite" in exp:
        checks["no_cite"] = not (cited_docs & set(exp["no_cite"]))
    oc = (final or {}).get("output_check") or {}
    composed = any(r[0] == "composer" and (r[2] or 0) > 0 for r in rows)   # an LLM answer, not a template
    raw_cites = set(_CITE.findall(raw_text))
    g = None
    if judge and composed and oc.get("verdict") == "allow":
        sources = "\n\n".join(f"[{h['chunk_id']}] {h['text']}" for h in st.get("hits", []))
        v, _ = get_llm().structured(JUDGE_SYSTEM, f"AGENT NOTE (what the customer said, as typed by the agent):\n{st.get('masked', '')}\n\nFACTS:\n{_facts(st)}\n\nSOURCES:\n{sources}\n\nANSWER:\n{ans}",
                                    Groundedness, model=JUDGE_MODEL)
        n = len(v.claims)
        g = {"claims": n, "supported": sum(c.supported for c in v.claims),
             "unsupported": [c.claim for c in v.claims if not c.supported],
             "facts": _facts(st)}   # kept so judge misses can be checked against what the judge saw (D61)
    audit = None
    if d:
        nodes = {r[0] for r in rows}
        action, evidence = draft_row if draft_row else ({}, [])
        audit = {"all_nodes": all(n in nodes for n in NODES_FOR_AUDIT),
                 "idv_recorded": "idv_level" in action, "evidence": bool(evidence),
                 "amount_from_lookup": any(e.get("tool") == "transaction_lookup" for e in evidence),
                 "rule_ids": bool(d.get("rule_ids")) or d["kind"] != "shadow_credit"}
    return {"id": sc["id"], "ok": all(checks.values()), "checks": checks, "intent": sup.get("intent"),
            "route": sup.get("route"), "draft": d and {k: d[k] for k in ("kind", "amount_inr", "approver_role")},
            "refusal": ((final or {}).get("refusal") or {}).get("rule_id"), "blocked": blocked,
            "citations": cited, "output_verdict": oc.get("verdict"), "violations": oc.get("violations", []),
            "raw_promise": bool(_unnegated_promise(raw_text)) if composed else False,
            "shipped_promise": bool(_unnegated_promise(ans)) and oc.get("verdict") == "allow",
            "premature_submission": bool(premature_submission(ans)) if composed else False,
            "raw_invented_citation": bool(raw_cites - retrieved), "composed": composed,
            "latency_s": round(latency, 2), "first_token_s": round(first_token, 2) if first_token else None,
            "tokens": sum((r[2] or 0) + (r[3] or 0) for r in rows),
            "cost_inr": round(sum(float(r[4] or 0) for r in rows), 4), "groundedness": g, "audit": audit,
            "answer": ans}


# --- routing (BANKING77 held-out) ---------------------------------------------------------

def routing_sample(n_total: int, seed: int = 20261125) -> list[dict]:
    intents = yaml.safe_load((ROOT / "rules" / "intents.yaml").read_text())["in_scope"]
    rows = list(csv.DictReader(open(ROOT / "data" / "banking77" / "test.csv", encoding="utf-8")))
    rng = random.Random(seed)
    per = max(1, int(n_total * 0.72) // len(intents))
    out = []
    for label in intents:
        pool = [r for r in rows if r["category"] == label]
        out += [{"text": r["text"], "label": label, "route": intents[label]["route"]} for r in rng.sample(pool, min(per, len(pool)))]
    oos = [r for r in rows if r["category"] not in intents]
    out += [{"text": r["text"], "label": "out_of_scope", "route": "refuse"} for r in rng.sample(oos, n_total - len(out))]
    return out


def run_routing(sample: list[dict], model: str) -> dict:
    from kestrel.graphs.chat import SUPERVISOR_SYSTEM, _intent_list, _kb_topics
    from kestrel.llm import SupervisorDecision, get_llm
    system = SUPERVISOR_SYSTEM.format(intents=_intent_list(), kb_topics=_kb_topics(TIER1))

    def one(s):
        t0 = time.perf_counter()
        d, u = get_llm().structured(system, s["text"], SupervisorDecision, model=model)
        pred = d.intent
        if d.intent == "out_of_scope" and d.route == "rag":   # D48: a KB topic with no intent label
            pred = "policy_question"
        return {**s, "pred": pred, "pred_route": "refuse" if pred == "out_of_scope" else d.route,
                "s": time.perf_counter() - t0, "cost_inr": u.cost_inr}

    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(one, sample))
    ins = [r for r in res if r["label"] != "out_of_scope"]
    oos = [r for r in res if r["label"] == "out_of_scope"]
    pred_in = [r for r in res if r["pred"] != "out_of_scope"]
    confusions: dict[str, int] = {}
    for r in res:
        if r["pred"] != r["label"]:
            k = f"{r['label']} -> {r['pred']}"
            confusions[k] = confusions.get(k, 0) + 1
    return {"n": len(res), "intent_acc": sum(r["pred"] == r["label"] for r in res) / len(res),
            "intent_acc_in_scope": sum(r["pred"] == r["label"] for r in ins) / len(ins),
            "route_acc_in_scope": sum(r["pred_route"] == r["route"] for r in ins) / len(ins),
            "scope_recall": sum(r["pred"] != "out_of_scope" for r in ins) / len(ins),
            "scope_precision": sum(r["label"] != "out_of_scope" for r in pred_in) / max(1, len(pred_in)),
            "oos_refused": sum(r["pred"] == "out_of_scope" for r in oos) / len(oos),
            "p50_s": statistics.median(r["s"] for r in res), "cost_inr_per_call": statistics.mean(r["cost_inr"] for r in res),
            "top_confusions": sorted(confusions.items(), key=lambda x: -x[1])[:8]}


# --- deadline sweeper simulation ---------------------------------------------------------

def deadline_sim(n: int = 60, seed: int = 11) -> dict:
    """Synthetic backlog: n cases with RBI-derived due dates from the real calculator rules,
    approved after a random supervisor delay (an ASSUMPTION: 0-14 calendar days, skewed
    short). Each simulated day runs the real TAT sweeper. Ground truth for a breach is
    computed independently (approval day > due date). Measures whether the sweeper
    detects every breach exactly once, and the breach rate *under the assumed delays*."""
    from psycopg.types.json import Jsonb

    from kestrel.db import connect
    from kestrel.jobs import tat_sweep
    from kestrel.rules.calculator import working_days_after   # noqa: F401 (documented dependency)
    from kestrel.rules.loader import load_rules
    rng = random.Random(seed)
    cal = load_rules().calendar
    start = ANCHOR
    cases = []
    with connect() as conn:
        conn.execute("DELETE FROM cases WHERE case_id LIKE 'CASE-SIM-%'")
        for i in range(n):
            submitted = start + timedelta(days=rng.randint(0, 9))
            due = submitted
            for _ in range(10):    # 10 working days (shadow-credit window), counted on the calendar
                due += timedelta(days=1)
                while due.weekday() == 6 or due in getattr(cal, "holidays", set()) or \
                        (due.weekday() == 5 and (due.day - 1) // 7 in (1, 3)):
                    due += timedelta(days=1)
            delay = min(int(rng.expovariate(1 / 5)), 30)
            cid = f"CASE-SIM-{i:03d}"
            cases.append({"id": cid, "submitted": submitted, "due": due, "approve_on": submitted + timedelta(days=delay)})
            conn.execute("INSERT INTO cases (case_id, status, risk_level, recommended_action, due_at) VALUES (%s, 'pending_approval', 'high', %s, %s)",
                         (cid, Jsonb({"sim": True}), due))
        conn.commit()
    flagged_breached, flagged_at_risk_first = set(), {}
    escalations = 0
    day = start
    last = max(c["approve_on"] for c in cases) + timedelta(days=2)
    while day <= last:
        with connect() as conn:
            for c in cases:
                if c["approve_on"] == day:
                    conn.execute("UPDATE cases SET status = 'approved' WHERE case_id = %s", (c["id"],))
            conn.commit()
        out = tat_sweep.run({"as_of": day.isoformat()}, lambda p: None)
        for ch in out["changes"]:
            if not ch["case_id"].startswith("CASE-SIM-"):
                continue
            if ch["to"] == "breached":
                flagged_breached.add(ch["case_id"])
            if ch["to"] == "at_risk":
                flagged_at_risk_first.setdefault(ch["case_id"], day)
        day += timedelta(days=1)
    with connect() as conn:
        escalations = conn.execute("SELECT count(*) FROM escalations WHERE case_id LIKE 'CASE-SIM-%'").fetchone()[0]
    truth = {c["id"] for c in cases if c["approve_on"] > c["due"]}
    leads = [(c["due"] - flagged_at_risk_first[c["id"]]).days for c in cases if c["id"] in truth and c["id"] in flagged_at_risk_first]
    return {"n": n, "breached_truth": len(truth), "breach_rate": len(truth) / n,
            "detected": len(flagged_breached & truth), "false_alarms": len(flagged_breached - truth),
            "escalations": escalations, "at_risk_warned_before_breach": sum(1 for c in truth if c in flagged_at_risk_first),
            "median_warning_days": statistics.median(leads) if leads else None,
            "assumed_delay": "exponential, mean 5 calendar days, capped at 30"}


# --- report --------------------------------------------------------------------------------

def pct(x):
    return "n/a" if x is None else f"{x:.0%}"


def summarise(results: list[dict]) -> dict:
    n = len(results)
    lat = sorted(r["latency_s"] for r in results if not r["blocked"])
    composed = [r for r in results if r["composed"]]
    graded = [r["groundedness"] for r in results if r["groundedness"]]
    audits = [r["audit"] for r in results if r["audit"]]
    p95 = lat[min(len(lat) - 1, int(round(0.95 * (len(lat) - 1))))] if lat else None
    return {
        "n": n, "pass": sum(r["ok"] for r in results),
        "intent_acc": _rate(results, "intent"), "route_acc": _rate(results, "route"),
        "outcome_acc": _rate(results, "draft"), "cite_acc": _rate(results, "cite"),
        "groundedness_claims": sum(g["supported"] for g in graded) / max(1, sum(g["claims"] for g in graded)) if graded else None,
        "fully_grounded_answers": sum(g["supported"] == g["claims"] for g in graded) / len(graded) if graded else None,
        "n_graded": len(graded),
        "withheld": sum(r["output_verdict"] == "block" for r in composed) / max(1, len(composed)),
        "raw_promise": sum(r["raw_promise"] for r in composed), "shipped_promise": sum(r["shipped_promise"] for r in results),
        "raw_invented_citation": sum(r["raw_invented_citation"] for r in composed), "n_composed": len(composed),
        "premature_submission": sum(r.get("premature_submission", False) for r in composed),
        "audit_complete": sum(all(a.values()) for a in audits) / len(audits) if audits else None, "n_drafts": len(audits),
        "p50_s": statistics.median(lat) if lat else None, "p95_s": p95,
        "first_token_p50_s": statistics.median([r["first_token_s"] for r in results if r["first_token_s"]] or [0]),
        "cost_inr_mean": statistics.mean(r["cost_inr"] for r in results),
        "tokens_mean": statistics.mean(r["tokens"] for r in results),
    }


def _rate(results, key):
    xs = [r["checks"][key] for r in results if key in r["checks"]]
    return (sum(xs) / len(xs), len(xs)) if xs else (None, 0)


def fmt_rate(t):
    v, n = t
    return "n/a" if v is None else f"{v:.0%} ({round(v * n)}/{n})"


def write_report(res: dict, out: Path) -> None:
    from kestrel.llm import PRICES_USD_PER_M, USD_INR
    L = ["# Evaluation report", "",
         f"Generated by `make eval` on {date.today().isoformat()}. Seed data anchored at {ANCHOR.isoformat()}; "
         f"{res['n_scenarios']} end-to-end scenarios (`evals/scenarios.yaml`), each run {res['repeats']} times per config "
         "(rates below are over all runs); groundedness judge (first repeat only): " + JUDGE_MODEL + ". "
         f"₹ costs use list prices verified 2026-11-25 ({', '.join(f'{m} ${a}/${b} per 1M in/out' for m, (a, b) in PRICES_USD_PER_M.items())}) "
         f"and an **assumed** rate of ₹{USD_INR:.0f}/USD.", ""]
    L += ["## Model comparison (Supervisor + Composer; rewrite stays gpt-5.4-nano)", "",
          "| Metric | " + " | ".join(CONFIGS[c]["label"] for c in res["configs"]) + " |",
          "|---|" + "---|" * len(res["configs"])]
    S = {c: res["configs"][c]["summary"] for c in res["configs"]}
    R = {c: res["configs"][c].get("routing") for c in res["configs"]}

    def row(name, f):
        L.append(f"| {name} | " + " | ".join(f(c) for c in res["configs"]) + " |")
    row(f"Scenarios passing all {res['repeats']} repeats", lambda c: f"{S[c]['all_repeats_pass']}/{S[c]['n_scenarios']}")
    row("…of which held-out scenarios (written after run 2, never tuned on)",
        lambda c: "{}/{}".format(*S[c].get("held_out", (0, 0))))
    row("Scenario runs passing", lambda c: f"{S[c]['pass']}/{S[c]['n']} ({S[c]['pass'] / S[c]['n']:.0%})")
    row("Intent correct (scenarios)", lambda c: fmt_rate(S[c]["intent_acc"]))
    row("Outcome correct (draft / no draft / amount / tier)", lambda c: fmt_rate(S[c]["outcome_acc"]))
    row("Cites the governing policy", lambda c: fmt_rate(S[c]["cite_acc"]))
    row("Groundedness: supported claims", lambda c: f"{pct(S[c]['groundedness_claims'])} over {S[c]['n_graded']} answers")
    row("Answers with zero unsupported claims", lambda c: pct(S[c]["fully_grounded_answers"]))
    row("Answers withheld by output check", lambda c: pct(S[c]["withheld"]))
    if all(R.values()):
        row("BANKING77 intent accuracy (held-out sample)", lambda c: f"{R[c]['intent_acc']:.0%} (n={R[c]['n']})")
        row("…in-scope intents only", lambda c: f"{R[c]['intent_acc_in_scope']:.0%}")
        row("Out-of-scope correctly refused", lambda c: f"{R[c]['oos_refused']:.0%}")
        row("In-scope wrongly refused (1 − scope recall)", lambda c: f"{1 - R[c]['scope_recall']:.0%}")
    row("Latency p50 / p95, full run", lambda c: f"{S[c]['p50_s']:.1f} s / {S[c]['p95_s']:.1f} s")
    row("Time to first answer token, p50", lambda c: f"{S[c]['first_token_p50_s']:.1f} s")
    row("Cost per run (mean)", lambda c: f"₹{S[c]['cost_inr_mean']:.3f} (${S[c]['cost_inr_mean'] / USD_INR:.4f})")
    row("Tokens per run (mean)", lambda c: f"{S[c]['tokens_mean']:.0f}")
    L += [""]
    p = S.get("prod")
    if p:
        L += ["## Ops and compliance metrics (production config)", "",
              "| Metric | Value | How measured |", "|---|---|---|",
              f"| Unapproved promises reaching the agent | **{p['shipped_promise']}** of {p['n']} runs | promise regex on the answer the agent saw |",
              f"| Unapproved promises the model *attempted* | {p['raw_promise']} of {p['n_composed']} composed answers | same regex on the raw stream, before the output check |",
              f"| Says the request is already with a supervisor (before the agent sends it) | {p.get('premature_submission', '–')} of {p['n_composed']} composed answers | `premature_submission` regex on the answer (D61) |",
              f"| Invented citations in the raw stream | {p['raw_invented_citation']} of {p['n_composed']} | cited chunk id not retrieved in that run (the postmortem bug) |",
              f"| Audit completeness of drafted actions | {pct(p['audit_complete'])} of {p['n_drafts']} drafts | every node in run_events, IDV level and evidence on the draft, amount from lookup, rule ids |",
              f"| Handling-time proxy | p50 {p['p50_s']:.1f} s per copilot answer | wall clock; the KB-STD-CALLS AHT target is 6 min per call |"]
        ds = res.get("deadline")
        if ds:
            L += [f"| TAT breaches detected by the sweeper | {ds['detected']}/{ds['breached_truth']}, {ds['false_alarms']} false alarms | synthetic backlog of {ds['n']} cases, sweeper run daily |",
                  f"| Breached cases warned 'at risk' first | {ds['at_risk_warned_before_breach']}/{ds['breached_truth']} (median {ds['median_warning_days']} days ahead) | same simulation |",
                  f"| Deadline-breach rate | {ds['breach_rate']:.0%} | **assumed** approval delay ({ds['assumed_delay']}); a property of staffing, not the software |"]
        L += [""]
    if res.get("retrieval"):
        from evals.retrieval_ablation import table
        L += ["## Retrieval ablation", "", f"{len(next(iter(res['retrieval'].values()))['misses']) and ''}"
              f"{next(iter(res['retrieval'].values()))['n']} gold queries (`evals/retrieval_gold.yaml`), document-level ranking, "
              "queries embedded as written (no rewrite).", "", table(res["retrieval"]), ""]
    for c in res["configs"]:
        L += [f"## Scenario detail: {CONFIGS[c]['label']}", "",
              "| Scenario | Passes | Checks failed (any repeat) | Intent | Route | Draft | Refusal | Cited | Latency | ₹ |",
              "|---|---|---|---|---|---|---|---|---|---|"]
        for p in res["configs"][c]["scenarios"]:
            r = p["first"]
            dr = f"{r['draft']['kind']} ₹{r['draft']['amount_inr']:,} ({r['draft']['approver_role']})" if r["draft"] else "–"
            L.append(f"| {p['id']} | {p['passes']}/{p['of']} | {', '.join(p['failed_checks'])} | "
                     f"{r['intent'] or ''} | {r['route'] or ''} | {dr} | {r['refusal'] or ''} | "
                     f"{', '.join(r['citations'])} | {r['latency_s']} s | {r['cost_inr']:.3f} |")
        L.append("\nIntent, route, draft, citations, latency and cost shown for the first repeat.")
        bad = [(p["id"], g["unsupported"]) for p in res["configs"][c]["scenarios"] if (g := p["first"]["groundedness"]) and g["unsupported"]]
        if bad:
            L += ["", "Unsupported claims found by the judge:", *[f"- {i}: " + "; ".join(u) for i, u in bad]]
        if R.get(c):
            L += ["", "Most common routing confusions (BANKING77 label -> predicted): " +
                  "; ".join(f"{k} ×{v}" for k, v in R[c]["top_confusions"])]
        L += [""]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--configs", default="prod,big")
    ap.add_argument("--routing-n", type=int, default=200)
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--repeats", type=int, default=3, help="runs per scenario (models are not deterministic)")
    ap.add_argument("--only", default="", help="comma-separated scenario ids")
    ap.add_argument("--out", default=str(ROOT / "reports" / "eval.md"))
    ap.add_argument("--report-only", action="store_true", help="re-render eval.md from eval.json")
    a = ap.parse_args()
    if a.report_only:
        out = Path(a.out)
        write_report(json.loads(out.with_suffix(".json").read_text()), out)
        print(out.read_text()); return
    setup_db()
    from kestrel.checkpoint import get_checkpointer
    import kestrel.graphs.chat as chat
    get_checkpointer.cache_clear(); chat._graph = None
    scenarios = yaml.safe_load((ROOT / "evals" / "scenarios.yaml").read_text())
    if a.only:
        scenarios = [s for s in scenarios if s["id"] in a.only.split(",")]
    res: dict = {"n_scenarios": len(scenarios), "repeats": a.repeats, "configs": {}}
    sample = routing_sample(a.routing_n) if a.routing_n else []
    for c in a.configs.split(","):
        os.environ.update({k: v for k, v in CONFIGS[c].items() if k.endswith("_MODEL") or k == "MODEL_PROFILE"})
        print(f"== {CONFIGS[c]['label']}", flush=True)
        rs, per = [], []
        for sc in scenarios:
            runs = []
            for i in range(a.repeats):
                r = run_scenario(sc, judge=not a.no_judge and i == 0)   # judge the first repeat only (cost)
                r["repeat"] = i
                print(f"  {'PASS' if r['ok'] else 'FAIL'} {r['id']}#{i} {r['checks']} {r['latency_s']}s", flush=True)
                runs.append(r)
            rs += runs
            per.append({"id": sc["id"], "held_out": bool(sc.get("held_out")), "passes": sum(r["ok"] for r in runs), "of": len(runs), "first": runs[0],
                        "failed_checks": sorted({k for r in runs for k, v in r["checks"].items() if not v})})
        res["configs"][c] = {"scenarios": per, "runs": rs, "summary": summarise(rs)}
        res["configs"][c]["summary"]["all_repeats_pass"] = sum(p["passes"] == p["of"] for p in per)
        res["configs"][c]["summary"]["n_scenarios"] = len(per)
        ho = [p for p in per if p["held_out"]]
        res["configs"][c]["summary"]["held_out"] = (sum(p["passes"] == p["of"] for p in ho), len(ho))
        if sample:
            res["configs"][c]["routing"] = run_routing(sample, CONFIGS[c]["SUPERVISOR_MODEL"])
            print("  routing", {k: v for k, v in res["configs"][c]["routing"].items() if k != "top_confusions"}, flush=True)
    os.environ.update({k: v for k, v in CONFIGS["prod"].items() if k.endswith("_MODEL") or k == "MODEL_PROFILE"})
    from evals import retrieval_ablation
    res["retrieval"] = retrieval_ablation.run()
    res["deadline"] = deadline_sim()
    out = Path(a.out)
    write_report(res, out)
    out.with_suffix(".json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    print(out.read_text())


if __name__ == "__main__":
    main()
