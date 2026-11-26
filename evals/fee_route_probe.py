"""Fee-routing probes (D58): the Supervisor's decision on held-out fee notes, plus the route mix on
BANKING77 fee-intent test rows. Supervisor only, no DB. ~₹2 per model at 3 repeats.

    python -m evals.fee_route_probe --models gpt-5.4-mini,gpt-5.4 --repeats 3
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import os
import random
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
FEE_LABELS = ["extra_charge_on_statement", "cash_withdrawal_charge", "card_payment_fee_charged", "transfer_fee_charged"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="gpt-5.4-mini,gpt-5.4")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--b77-n", type=int, default=40, help="BANKING77 fee-intent test rows (0 to skip)")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    os.environ.setdefault("LLM_PROVIDER", "openai")
    from kestrel.access import TIER1
    from kestrel.graphs.chat import SUPERVISOR_SYSTEM, _intent_list, _kb_topics
    from kestrel.llm import SupervisorDecision, get_llm
    system = SUPERVISOR_SYSTEM.format(intents=_intent_list(), kb_topics=_kb_topics(TIER1))
    probes = yaml.safe_load((ROOT / "evals" / "fee_route_probes.yaml").read_text())
    with open(ROOT / "data" / "banking77" / "test.csv", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["category"] in FEE_LABELS]
    b77 = random.Random(20261126).sample(rows, min(a.b77_n, len(rows)))
    out = {}
    for model in a.models.split(","):
        def one(job, model=model):
            kind, text = job
            d, _ = get_llm().structured(system, text, SupervisorDecision, model=model)
            return {"kind": kind, "text": text, "intent": d.intent, "route": d.route, "action": d.proposed_action}
        jobs = [(k, t) for k in ("action", "no_action") for t in probes[k] for _ in range(a.repeats)]
        jobs += [("b77", r["text"]) for r in b77]
        with ThreadPoolExecutor(8) as ex:
            res = list(ex.map(one, jobs))
        ok_action = [r for r in res if r["kind"] == "action"]
        ok_no = [r for r in res if r["kind"] == "no_action"]
        hit = sum(r["route"] == "action" for r in ok_action)
        clean = sum(r["route"] in ("rag", "tools") for r in ok_no)
        b = [r for r in res if r["kind"] == "b77"]
        summary = {"action_probes_routed_action": f"{hit}/{len(ok_action)}",
                   "no_action_probes_routed_rag_or_tools": f"{clean}/{len(ok_no)}",
                   "b77_fee_route_mix": dict(collections.Counter(r["route"] for r in b)),
                   "misses": [r for r in ok_action if r["route"] != "action"] +
                             [r for r in ok_no if r["route"] not in ("rag", "tools")],
                   "b77_action_examples": [r["text"] for r in b if r["route"] == "action"][:10]}
        out[model] = summary
        print(model, json.dumps({k: v for k, v in summary.items() if k != "misses"}, ensure_ascii=False))
        for m in summary["misses"]:
            print("  MISS", m["kind"], m["route"], m["action"], "|", m["text"][:90])
    if a.out:
        Path(a.out).write_text(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
