"""Accuracy of the Enron compliance scan against a labelled sample.

Labels: evals/compliance/labels.csv (method in evals/compliance/README.md). Two strata:
  flagged           every message the M3 scanner flagged (census, weight 1)
  random_unflagged  200 messages drawn at random (seed 20261125) from the 1,974 it did not flag
                    (each stands for 1,974 / 200 = 9.87 messages)

Precision and recall are estimated for the *whole 2,000-message population* with those
weights, so the enriched sample doesn't inflate recall. 95% intervals are a stratified
bootstrap (2,000 resamples). Runs the current scanner code, so it also measures any
later change to the rules on the same labels (an in-sample number; see the README).

    python -m evals.compliance_eval [--labels strict|lenient] [--out reports/compliance-scan.md]
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import random
from collections import Counter
from pathlib import Path

from kestrel.config import ROOT, get_settings
from kestrel.jobs.enron_scan import scan_message

LABELS = ROOT / "evals" / "compliance" / "labels.csv"
POP_FLAGGED, POP_UNFLAGGED, N_RANDOM = 26, 1974, 200


def load(label_set: str):
    msgs = [json.loads(line) for line in gzip.open(get_settings().enron_sample, "rt", encoding="utf-8")]
    rows = list(csv.DictReader(open(LABELS, encoding="utf-8")))
    out = []
    for r in rows:
        m = msgs[int(r["idx"])]
        assert m["message_id"] == r["message_id"], "labels and sample out of sync"
        flags = {f["rule_id"] for f in scan_message(m)}
        truth = {x for x in r[label_set].split(";") if x}
        out.append({**r, "pred": bool(flags), "flags": flags, "truth": truth, "pos": bool(truth)})
    return out


def weights(rows):
    nf = sum(r["stratum"] == "flagged" for r in rows)
    nr = sum(r["stratum"] == "random_unflagged" for r in rows)
    assert nf == POP_FLAGGED and nr == N_RANDOM
    return {"flagged": 1.0, "random_unflagged": POP_UNFLAGGED / nr}


def estimate(rows, w):
    tp = sum(w[r["stratum"]] for r in rows if r["pred"] and r["pos"])
    fp = sum(w[r["stratum"]] for r in rows if r["pred"] and not r["pos"])
    fn = sum(w[r["stratum"]] for r in rows if not r["pred"] and r["pos"])
    p = tp / (tp + fp) if tp + fp else float("nan")
    rec = tp / (tp + fn) if tp + fn else float("nan")
    return {"tp": tp, "fp": fp, "fn": fn, "precision": p, "recall": rec, "flagged_est": tp + fp}


def bootstrap(rows, w, n=2000, seed=7):
    rng = random.Random(seed)
    strata = {s: [r for r in rows if r["stratum"] == s] for s in w}
    ps, rs = [], []
    for _ in range(n):
        res = [rng.choice(strata[s]) for s in strata for _ in strata[s]]
        e = estimate(res, w)
        if e["precision"] == e["precision"]:
            ps.append(e["precision"])
        if e["recall"] == e["recall"]:
            rs.append(e["recall"])
    q = lambda xs: (sorted(xs)[int(0.025 * len(xs))], sorted(xs)[int(0.975 * len(xs)) - 1]) if xs else (float("nan"),) * 2
    return q(ps), q(rs)


def per_rule(rows):
    """Per-rule precision over every message the current code flags in the labelled set."""
    out = {}
    for rule in sorted({f for r in rows for f in r["flags"]}):
        hits = [r for r in rows if rule in r["flags"]]
        ok = sum(rule in r["truth"] for r in hits)
        out[rule] = (ok, len(hits))
    return out


def report(label_set: str) -> tuple[str, dict]:
    rows = load(label_set)
    w = weights(rows)
    e = estimate(rows, w)
    (plo, phi), (rlo, rhi) = bootstrap(rows, w)
    pos_lab = sum(r["pos"] for r in rows)
    hit_lab = sum(r["pos"] and r["pred"] for r in rows)
    missed = [r for r in rows if r["pos"] and not r["pred"]]
    pr = per_rule(rows)
    lines = [
        f"### Labels: {label_set}",
        "",
        f"- Flagged now: **{sum(r['pred'] for r in rows if r['stratum']=='flagged')} of 26** baseline-flagged messages still flagged; "
        f"{sum(r['pred'] for r in rows if r['stratum']=='random_unflagged')} of the 200 random unflagged messages are flagged by the current code.",
        f"- **Precision ≈ {e['precision']:.0%}** (95% CI {plo:.0%}–{phi:.0%}).",
        f"- **Recall ≈ {e['recall']:.0%}** (95% CI {rlo:.0%}–{rhi:.0%}), estimated over all 2,000 messages "
        f"(≈{e['tp']:.0f} true positives found vs ≈{e['fn']:.0f} missed).",
        f"- Naive recall on the labelled set alone: {hit_lab}/{pos_lab} = {hit_lab/pos_lab:.0%}. "
        "This is inflated: the set contains every flagged message but only 10% of the unflagged ones.",
        "",
        "| Rule | Correct flags / flags (labelled set) |",
        "|---|---|",
        *[f"| {k} | {ok}/{n} |" for k, (ok, n) in pr.items()],
        "",
        "Positives the current code misses (in the labelled set):",
        *[f"- #{r['idx']} ({', '.join(sorted(r['truth']))}): {r['note']}" for r in missed],
        "",
    ]
    return "\n".join(lines), {"label_set": label_set, **e, "precision_ci": [plo, phi], "recall_ci": [rlo, rhi],
                              "per_rule": pr}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "reports" / "compliance-scan.md"))
    a = ap.parse_args()
    parts, data = [], {}
    for ls in ("strict", "lenient"):
        text, d = report(ls)
        parts.append(text); data[ls] = d
    body = ("## Measured accuracy (generated by `python -m evals.compliance_eval`)\n\n"
            "Sample: 226 labelled messages (26 flagged + 200 random unflagged) out of 2,000. "
            "Labelling method and caveats: `evals/compliance/README.md`.\n\n" + "\n".join(parts))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(body, encoding="utf-8")
    Path(a.out).with_suffix(".json").write_text(json.dumps(data, indent=2, default=list), encoding="utf-8")
    print(body)


if __name__ == "__main__":
    main()
