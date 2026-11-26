---
id: KB-SOP-IDV
title: Identity Verification SOP for Phone Banking
document_type: sop
department: contact_centre
sensitivity: restricted
audience: [all_staff]
effective_from: 2025-02-01
status: current
---

# Identity Verification for Phone Banking

| Level | Checks | Allows |
|---|---|---|
| 1 | Registered mobile number match **and** date of birth | Card block, balance enquiry, status of an existing ticket |
| 2 | Level 1 **plus** an OTP sent to the registered mobile | Raising disputes, drafting credits, fee reversals, chargebacks |
| 3 | Level 2 **plus** video KYC or branch visit | Changing contact details, mobile number or address |

## Rules
- Never ask for a full card number, CVV, PIN, OTP for a transaction, or net-banking password. Ask only for the last four digits of the card.
- If verification fails, the copilot refuses to draft any action (rule KB-REF-1). Offer a branch visit.
- A request to change contact details from an unverified caller is refused outright (KB-REF-2), even if the caller sounds urgent. Urgency is a common social-engineering signal.
