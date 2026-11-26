---
id: KB-POL-REFUND
title: Merchant Refunds and Reverted Card Payments
document_type: policy
department: disputes
sensitivity: internal
audience: [all_staff]
effective_from: 2025-01-10
status: current
---

# Merchant Refunds and Reverted Card Payments

## Merchant refunds
A refund initiated by a merchant normally reaches the customer's account **5 to 7 working days** after the merchant processes it, depending on the merchant's acquiring bank. Kestrel credits the refund the same day it receives it from the card network.

If a customer says a merchant refund has not arrived:
1. Ask for the merchant's refund reference (ARN) and refund date.
2. If fewer than 7 working days have passed, explain the timeline; no action needed.
3. If more than 7 working days have passed and the customer has an ARN, open a refund trace ticket (category `refund_trace`). Do not promise a date.
4. If there is no ARN, advise the customer to contact the merchant first. If the merchant refuses, a chargeback may be possible (KB-SOP-CHARGEBACK).

## Reverted card payments
A pending card payment that a merchant never completes is released automatically, usually within 7 calendar days. A reversal appears as a separate credit on the statement.

## Goodwill refunds by the bank
Any refund or credit paid by Kestrel itself, of any amount, requires supervisor approval (KB-MATRIX). Tier-1 agents may never promise one.
