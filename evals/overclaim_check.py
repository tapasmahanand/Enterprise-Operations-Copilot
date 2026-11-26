"""D61 check: does the answer claim the request is already with a supervisor while the draft is unsent?
Runs evals/overclaim_probes.yaml (held-out notes that produce drafts) through the full graph at IDV 2,
in its own database (kestrel_scope). No judge. ~₹30 for both configs at 3 repeats.

    python -m evals.overclaim_check --out reports/overclaim-after.json
"""
from __future__ import annotations

import argparse
import json
import os

import yaml

from evals.fee_scope_check import setup_scope_db
from evals.run_eval import CONFIGS, ROOT


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--configs", default="prod,big")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    setup_scope_db()
    from evals.run_eval import run_scenario
    from kestrel.guards.output import premature_submission
    notes = yaml.safe_load((ROOT / "evals" / "overclaim_probes.yaml").read_text())
    out = {}
    for c in a.configs.split(","):
        os.environ.update({k: v for k, v in CONFIGS[c].items() if k.endswith("_MODEL") or k == "MODEL_PROFILE"})
        runs = []
        for rep in range(a.repeats):
            for i, note in enumerate(notes):
                r = run_scenario({"id": f"O{i + 1:02d}", "note": note, "idv": 2, "expect": {}}, judge=False)
                m = premature_submission(r["answer"] or "")
                runs.append({"id": r["id"], "repeat": rep, "draft": r["draft"], "composed": r["composed"],
                             "premature": m.group(0) if m else None, "answer": r["answer"]})
        composed = [r for r in runs if r["composed"]]
        out[c] = {"composed": len(composed), "with_draft": sum(bool(r["draft"]) for r in composed),
                  "premature": sum(bool(r["premature"]) for r in composed), "runs": runs}
        print(c, {k: v for k, v in out[c].items() if k != "runs"}, flush=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, default=str)


if __name__ == "__main__":
    main()
