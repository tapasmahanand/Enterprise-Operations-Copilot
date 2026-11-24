---
id: KB-POL-TAT
title: Failed Transactions — Turnaround Time and Customer Compensation
document_type: policy
department: payments_ops
sensitivity: internal
effective_from: 2024-07-01
status: current
rbi_reference: RBI/2019-20/67, DPSS.CO.PD No.629/02.01.014/2019-20 (20 September 2019)
---

# Failed Transactions — Turnaround Time (TAT) and Compensation

Aligned with the RBI circular harmonising TAT and customer compensation for failed transactions. **T** is the calendar date of the transaction. Compensation is credited **automatically, without waiting for a complaint**.

| Channel | Failure | Auto-reversal by | Compensation if late |
|---|---|---|---|
| ATM (incl. micro-ATM) | Account debited, cash not dispensed | T + 5 days | ₹100 per day of delay beyond T + 5 |
| Card to card transfer | Card debited, beneficiary not credited | T + 1 day | ₹100 per day beyond T + 1 |
| PoS (card present) | Debited, merchant got no confirmation | T + 5 days | ₹100 per day beyond T + 5 |
| IMPS | Debited, beneficiary not credited | T + 1 day | ₹100 per day beyond T + 1 |
| UPI transfer | Debited, beneficiary not credited | T + 1 day | ₹100 per day beyond T + 1 |
| UPI merchant payment | Debited, merchant got no confirmation | T + 5 days | ₹100 per day beyond T + 5 |

**Card-not-present (e-commerce) failures:** route to payments operations; tier-1 agents do not quote a TAT for this channel.

## Agent guidance
- Look up the transaction and confirm its status is "failed, debited".
- Tell the customer the reversal date from the table. If it has passed, tell them compensation is credited automatically at ₹100 per day of delay.
- A customer who is not satisfied can escalate (KB-SOP-ESC), and can approach the RBI Integrated Ombudsman.
