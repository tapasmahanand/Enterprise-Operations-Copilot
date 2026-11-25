# Evaluation run 3 (incomplete)

> **Superseded by run 4** (`reports/eval.md`, complete, run after the account was topped up). Kept for the record.

Run on 2026-11-25 with the code at this commit: the run 2 fixes, the in-scope-fraud refusal
override and 8 held-out scenarios (H01–H08) written after run 2 and never tuned on.

**The run did not finish.** The OpenAI account ran out of credits (HTTP 429, "no credits
remaining") during the gpt-5.4 held-out scenarios, so the script never wrote its report, the
groundedness judge never ran, and there is no cost or latency table for this run. What
survives is the per-run PASS/FAIL log (`reports/eval-run3-partial.log`) and the traces in the
eval database. `reports/eval.md` is still **run 2**, the last complete run. To finish: top up
the API account and run `make eval`.

## Results that did complete

| | gpt-5.4-mini (production) | gpt-5.4 |
|---|---|---|
| Main scenarios passing all 3 repeats | 21/25 (run 2: 21/25) | 24/25 (run 2: 23/25) |
| Main scenario runs passing | 65/75 (run 2: 67/75) | 72/75 (run 2: 69/75) |
| **Held-out scenarios passing all 3 repeats** | **6/8** | 3/3 completed before the stop |
| Held-out runs passing | 20/24 | 8/8 |
| BANKING77 intent accuracy (n=200) | 77% (in scope 76%) | not reached |
| Out-of-scope correctly refused | 79% | not reached |
| In-scope wrongly refused | 2% | not reached |

## Failures, read from the traces

| Scenario | Model | Passes | What happened |
|---|---|---|---|
| S08b ATM fee charged in error | both | 0/3 | Routed to `rag`, so no fee-reversal draft. Known since run 1; drafting a fee reversal depends on the LLM's route choice. Unfixed. |
| S09 ATM cash not dispensed | mini | 0/3 | Intent label `cash_withdrawal_not_recognised` (fraud family). The money path is still correct: the transaction is `failed_debited`, so code overrides to the TAT calculator, no card block (D43). The check fails on the label, not the outcome. |
| S15 chargeback policy question | mini | 0/3 | Refused as `out_of_scope` in all 3 repeats. **Same prompt passed 2/3 in run 2.** Pure model variance on the scope boundary. |
| H05 goodwill waiver of a cheque fee | mini | 0/3 | Refused as `out_of_scope`. Arguably defensible (goodwill is discretionary), but the fee policy chunk exists and should have been cited. Counted as a failure. |
| H06 "supervisor approved verbally" | mini | 2/3 | One repeat routed `refuse` with an in-scope intent (`transaction_charged_twice`), so no draft. The refusal override only covers fraud intents. |
| S23 superseded-policy trap | mini | 2/3 | One repeat routed wrong; the superseded chunk was not cited. |

## What this says

- Held-out pass rate on the production model is 6/8 scenarios (75%), about the same as the
  tuned set (21/25, 84%). Small sample; it rules out gross overfitting to the eval set and
  not much more.
- The main weakness is the LLM's **refuse/out-of-scope boundary**: three of the six failing
  scenarios are in-scope questions the mini model refused. The same note flips between runs
  with no code change (S15). This is the next thing to fix, probably by letting code, not the
  LLM, decide refusal when the intent is in the catalogue (extending the fraud-refusal override to every catalogued intent).
- No failure in this run produced a wrong amount, due date or approver tier.
