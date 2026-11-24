---
id: KB-GUIDE-PII
title: Handling Personal and Sensitive Data
document_type: guideline
department: compliance
sensitivity: internal
effective_from: 2025-05-01
status: current
---

# Handling Personal and Sensitive Data

Agents often hear personal data on calls. It must never be stored or sent anywhere in full.

## Data that must be masked
| Data | Example | Store as |
|---|---|---|
| Card number (PAN) | 4111 1111 1111 1234 | last 4 digits only (`XXXX-1234`) |
| Aadhaar number | 1234 5678 9012 | `XXXX-XXXX-9012` |
| PAN (tax id) | ABCDE1234F | `XXXXX1234X` |
| Mobile number | +91 98765 43210 | last 4 digits |
| Email address | name@example.com | domain only |
| CVV, PIN, OTP, passwords | — | **Never record at all** |

## Rules
- The copilot masks these before storing, logging or sending text to any model.
- If a customer reads out a CVV, PIN or OTP, tell them never to share it, mask it, and treat the card as compromised (KB-SOP-BLOCK).
- Never paste raw customer data into tools outside the bank's approved systems.
- Compliance scans mailboxes and tickets for unmasked personal data; findings are reported to the Data Protection Officer.
