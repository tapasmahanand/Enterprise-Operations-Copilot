"""Groundedness on notes the composer was never tuned on (D59): the valid items of one scope-set split,
through the full chat graph, judged with the same judge and rubric as `make eval`. Runs in its own
database (kestrel_scope), so it can run beside `make eval`. ~₹50 for test3 on both configs.

    python -m evals.grounding_check --split test3 --out reports/grounding-after.json

Each unsupported claim is also bucketed: a workflow step ("review the draft", "Send to supervisor")
said when this run made a draft, the same said when it made none, or anything else.
"""
from __future__ import annotations

import argparse
import json
import os
import re

import yaml

from evals.fee_scope_check import setup_scope_db
from evals.run_eval import CONFIGS, ROOT

WORKFLOW = re.compile(r"send to supervisor|review (the |this |it|draft)|click|\bdraft", re.I)


def bucket(res: list[dict]) -> dict:
    judged = [r for r in res if r["groundedness"]]
    out = {"answers": len(judged), "zero_unsupported": sum(not r["groundedness"]["unsupported"] for r in judged),
           "claims": sum(r["groundedness"]["claims"] for r in judged),
           "supported": sum(r["groundedness"]["supported"] for r in judged),
           "workflow_with_draft": 0, "workflow_no_draft": 0, "other": 0}
    for r in judged:
        for u in r["groundedness"]["unsupported"]:
            k = "other" if not WORKFLOW.search(u) else "workflow_with_draft" if r["draft"] else "workflow_no_draft"
            out[k] += 1
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="test3")
    ap.add_argument("--configs", default="prod,big")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    setup_scope_db()
    from evals.run_eval import run_scenario
    items = [i for i in yaml.safe_load((ROOT / "evals" / "scope_set.yaml").read_text())["items"]
             if i["split"] == a.split and i["label"] == "valid"]
    out = {}
    for c in a.configs.split(","):
        os.environ.update({k: v for k, v in CONFIGS[c].items() if k.endswith("_MODEL") or k == "MODEL_PROFILE"})
        res = [run_scenario({"id": i["id"], "note": i["note"], "idv": i.get("idv", 0), "expect": {}}, judge=True)
               for i in items]
        out[c] = {"summary": bucket(res), "runs": res}
        print(c, json.dumps(out[c]["summary"]), flush=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, default=str)


if __name__ == "__main__":
    main()
