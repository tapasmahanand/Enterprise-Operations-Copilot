# Compliance-scan accuracy: labelled sample

The M3 scan reported "26 of 2,000 emails flagged". That number says nothing about whether
the flags are right or what was missed. This folder holds a labelled sample and
`evals/compliance_eval.py` turns it into precision and recall.

## Sample

| Stratum | Size | How chosen | Weight in estimates |
|---|---|---|---|
| `flagged` | 26 | every message the M3 scanner flagged (a census) | 1 |
| `random_unflagged` | 200 | uniform random draw, seed `20261125`, from the 1,974 messages it did not flag | 1,974 / 200 = 9.87 |

Precision and recall are estimated for the whole 2,000-message population with those weights.
A naive recall on the 226 labelled messages is also printed and is **inflated** (the sample holds
all the flags but only 10% of the non-flags); don't quote it.

## Who labelled, and how

- **Labeller:** Claude (the assistant that built the system), not a compliance analyst, and only one
  labeller, so there is no inter-annotator agreement figure. Treat these as provisional labels;
  `labels.csv` has a one-line reason for every positive and every flagged message so a human can
  re-label quickly. Re-running the script after editing the CSV updates every number.
- **What was read:** for each message, the subject, the recipient domains (parsed by address), the
  first ~450 characters, and every passage around a broad keyword list over the *whole* body
  (password, passcode, PIN, login, user id, SSN, social security, account/card number, delete,
  destroy, shred, confidential, privileged, any 9+ digit run). A violation far from any of those
  words could have been missed, so recall may be slightly *over*-estimated.
- **Label definitions** (what each rule claims to detect):
  - `CS-CRED`: an actual secret value that opens a system or account (login password, user id +
    password). **Strict** excludes conference-bridge passcodes and pager PINs, which are routinely
    broadcast in invites; **lenient** counts them. Describing a password scheme ("your password is
    your date of birth") without a value is negative in both.
  - `CS-EXT-CONF`: the author marked the content confidential/privileged (subject or body, not a
    legal footer) **and** at least one recipient is outside enron.com. Whether the disclosure was
    legitimate (outside counsel, joint-defence groups) is not judged; that is for the reviewer.
  - `CS-DESTROY`: an instruction to delete or destroy records. Legal footers and commentary
    ("even if you order your accounting firm to shred...") are negative.
  - `CS-PII-KB`: a real card, Aadhaar or PAN number. Order numbers, MIME boundaries, URL digits
    are negative.
  - `CS-PII-SSN`, `CS-PII-ACCT`: a real SSN / bank account or card number. None found in the sample.

## Results

Baseline (M3 code, `reports/compliance-scan-baseline.md`), strict labels:

| | Estimate | 95% CI |
|---|---|---|
| Precision | 38% (10 of 26 flagged messages correct) | 19–58% |
| Recall | ~20% (≈10 found, ≈39 missed of 2,000) | 9–57% |

Per rule: CS-PII-KB 0/9 correct (12-digit order numbers, MIME boundaries and URL digits read as
Aadhaar or card numbers), CS-CRED 3/9 (6/9 lenient), CS-EXT-CONF 7/9, CS-DESTROY 0/1.

After the fixes in this milestone (`reports/compliance-scan.md`), strict labels: precision ≈71%
(38–89%), recall ≈40% (13–100%). **These are in-sample numbers:** the fixes were made after reading
the errors in this same sample, so they are optimistic. The fixes are principled rather than
tuned to individual messages (recipient parsing bug, checksum validation of ID numbers, ignoring
"confidential" inside footers), but an honest post-fix figure needs a fresh labelled sample.

## What was wrong and what changed

1. **Recipient parsing bug (recall).** To and Cc are joined with a space and many lists are
   space-separated; splitting on commas glued `x@caiso.com y@enron.com` into one token that "ends
   with enron.com", so external recipients were invisible. Found through #1224 in the random
   sample; after the fix two more unlabelled messages (#1675, #1713: privileged legal advice sent to
   gspcorp.com and aol.com) were flagged, and both are genuine.
2. **CS-PII-KB was pure noise (precision).** It reused chat masking, which deliberately over-masks.
   The scan now requires a Luhn-valid card or Verhoeff-valid Aadhaar not embedded in a URL or id.
   Masking in chat is unchanged: over-masking there is cheap, a false scan flag costs review time.
3. **Footers counted as markings.** "This message may contain confidential information..." in a
   short message's signature block triggered CS-EXT-CONF (#1737). Now ignored.

## Still wrong (not fixed, on purpose or for lack of time)

- Recall is poor and badly measured. The random sample found only 4 misses (strict), so the
  recall interval is very wide. Misses are phrased in ways a regex won't anticipate:
  "login and password for EPMI is <user> <pw>" (#1198), "all audio tapes older than six months
  will be physically destroyed" while litigation is pending (#1717, the most important miss in
  the sample), a privileged marking quoted below a reply (#993).
- CS-CRED still flags a newspaper headline and two HR mails that describe the password scheme.
- **Better approach:** keep regex as a high-recall pre-filter with loose patterns, then let a
  small LLM classifier (or a human queue) decide, and measure both stages on a larger, double-
  labelled sample (≥500 random messages; two labellers; report Cohen's kappa).
