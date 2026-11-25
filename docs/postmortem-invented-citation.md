# Postmortem: the Composer cited a policy chunk it was never given

*Blameless format. Severity: low (caught by a control before any customer impact). Written 2026-11-25 for Milestone 4.*

## Summary

During Milestone 2 development (24 Nov 2026), on the hero case (an unrecognised ₹18,450 card
payment), the Composer's answer cited `[KB-POL-LIAB-A:0]`, a chunk of the correct liability
policy that **had not been retrieved in that run**. The output check (`kestrel/guards/output.py`)
caught it: every cited chunk id must be in the run's retrieved set. The answer was withheld and
the agent saw the fallback: "[The drafted answer was withheld by the output check: cites chunks
that were not retrieved: KB-POL-LIAB-A:0]" followed by the raw facts from bank systems.

No customer was affected. The case still drafted correctly, because the draft, amount and due
date come from code, not from the answer.

## Why it matters even though the control worked

- A citation is the agent's (and an auditor's) evidence that a statement is policy. A citation to
  text the model never saw is a fabricated source, even when the chunk exists and happens to say
  the right thing. In a bank, "the copilot cited a policy it hadn't read" is an audit finding.
- The fallback is safe but bad for the agent on a live call: a bracketed error and a list of facts
  instead of guidance.
- Answers stream token by token and the check runs at the end (D26). For the ~2 s of streaming,
  the agent could read the uncited answer before it was replaced. The control prevents the answer
  from being *kept*, not from being *seen*.

## Timeline (24 Nov 2026)

1. Hero demo running end to end against the real model for the first time.
2. A run ends with the "withheld by the output check" fallback; the trace (`compliance_out`
   node, status `blocked`) names `KB-POL-LIAB-A:0` as cited but not retrieved.
3. The prompt is changed: the Composer's user message now ends with "You may cite ONLY these chunk
   ids, copied exactly: <the ids retrieved in this run>" (`kestrel/graphs/chat.py`, `composer`).
4. `scripts/hero_repeat.py 20`: 0 of 20 runs withheld.

**What we don't know:** how often it happened before the fix. Withheld runs were noticed, not
counted, so there is no before-rate. That gap is itself one of the findings below.

## Root cause (most likely; it could not be reproduced afterwards, see below)

The Composer was shown chunks labelled `[chunk_id=KB-POL-LIAB-A:1]` and told to cite "exactly as
given, e.g. [KB-POL-LIAB-A:0]". The example id in the system prompt was a real chunk id. When
retrieval returned a sibling chunk (`:1`) but not `:0`, the model had both the document name and
an example of the exact citation format for `:0` in its context, and produced it. The model
pattern-completed a plausible id rather than copying one from the context.

Contributing factors:

- **The prompt used a real id as its formatting example.** The example doubled as a suggestion.
- **Nothing told the model which ids were allowed**; it had to infer that from chunk headers.
- **Retrieval quality on the hero query was weaker then.** The intent-policy boost and date
  filter (D21, D13) were still being tuned, so the governing chunk was not always in the top 5.

## Detection

The deterministic citation check in `compliance_out`. It was designed in from Milestone 2
(design §5.1) exactly for this, and it worked the first time it was needed. Detection was
**immediate and automatic**, which is the main thing that went right.

## Reproduction attempt (25 Nov 2026), and what it says about the fix

`python -m evals.repro_invented_citation 20` runs the hero case against the current code, with
and without the "cite ONLY these ids" instruction, and also with `KB-POL-LIAB-A:0` forcibly
removed from the retrieved set to recreate the incident's condition:

| Condition | Allowed-id list in prompt | Runs | Invented citations | Withheld |
|---|---|---|---|---|
| normal retrieval | no | 20 | 0 | 0 |
| normal retrieval | yes | 20 | 0 | 0 |
| `:0` removed from retrieval | no | 20 | 0 | 0 |
| `:0` removed from retrieval | yes | 20 | 0 | 0 |

The incident does **not** reproduce today, even without the fix and even in the triggering
condition. So the honest conclusion is: **the 0/20 after the fix cannot be credited to the fix.**
Other things changed between the incident and now (retrieval tuning, the composer prompt's
wording, and possibly the model snapshot behind the `gpt-5.4-mini` alias). The full eval agrees:
0 invented citations in the raw stream across every scenario run (`reports/eval.md`).

The prompt change is still worth keeping (it is cheap and removes an obvious ambiguity), but the
control that actually protects the system is the deterministic check, and it should be treated
as the primary defence, not a backstop.

## What we changed

| Change | Status |
|---|---|
| Allowed chunk ids listed in the Composer prompt | Done (M2) |
| Raw-stream invented-citation rate measured on every `make eval` run, before the output check | Done (M4, `reports/eval.md`, "Invented citations in the raw stream") |
| Reproduction script kept in the repo so the claim can be re-checked when the model changes | Done (M4) |

## What we should still change (not done)

1. **Replace the real id in the system prompt's example** with an obviously fake one
   (`[DOC-ID:0]`). Not done in M4 because it would change the prompt mid-evaluation; it is the
   first item for the next prompt revision.
2. **Count every output-check block** as a metric with an alert threshold (for example > 2% of runs
   in an hour), instead of relying on someone noticing. `run_events` already has the data
   (`compliance_out`, status `blocked`); it needs a query and an alert.
3. **Repair instead of withhold**: when the only violation is an unretrieved citation, strip that
   citation and re-check, or re-ask the Composer once with the violation named. Today the agent
   gets a fallback on a live call for what may be a one-character error.
4. **Decide on streaming before checking (D26).** If the bank's risk owner says an unchecked answer
   must never be visible, buffer and check first and accept ~2 s more latency (p50 first token is
   already ~3 s, `reports/eval.md`).

## Lessons

- A deterministic output check paid for itself on its first real failure. LLM-written citations
  need machine verification; "the prompt says to cite only retrieved chunks" is not a control.
- Fixes to LLM behaviour need a before-rate, an after-rate and a reproduction. We had only the
  after-rate, and the reproduction later showed it could not distinguish the fix from other changes.
- Measuring the raw stream (before guards) separately from what the user sees tells you whether
  the guard is doing work. Both numbers belong in the eval report.
