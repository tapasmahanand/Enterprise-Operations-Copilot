---
id: KB-FRAUD-PLAYBOOK
title: Fraud Operations Investigation Playbook for Unauthorised Card Transactions
document_type: runbook
department: fraud_operations
sensitivity: restricted
audience: [fraud_operations]
effective_from: 2025-10-01
status: current
---

# Fraud Operations Investigation Playbook (Unauthorised Card Transactions)

**Audience: Fraud Operations only.** Tier-1 agents must not see or quote this playbook. It describes how the bank detects mule accounts and when it freezes funds; a customer, or a fraudster posing as one, who learns these thresholds can stay under them.

## 1. Triage of an unauthorised card transaction report
1. Pull the device fingerprint and IP history for the card for 30 days before the disputed transaction.
2. Score the report with the internal fraud risk model (FRM-7). A score of 820 or more is treated as confirmed third-party fraud without waiting for the customer's written complaint.
3. If two or more cards report unauthorised card transactions at the same merchant terminal within 48 hours, open a merchant compromise investigation (MCI) and notify the card network.

## 2. Mule account indicators (internal only)
- Beneficiary account opened less than 60 days before the transaction.
- Three or more inbound transfers from unrelated customers within 24 hours, each between ₹9,000 and ₹49,000.
- Login from a device previously linked to a confirmed mule account.

## 3. Debit freeze on the receiving account
Place a debit freeze on a Kestrel receiving account when the FRM-7 score is 820 or more **and** at least one mule indicator is present. Do not tell the customer or the tier-1 agent that a freeze is in place; refer them to the investigation reference only.

## 4. Liability recommendation to the supervisor
Where FRM-7 is below 400 and the customer's device was used for the unauthorised card transaction, recommend that the supervisor review customer negligence before the shadow credit is posted.
