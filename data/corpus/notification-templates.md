---
id: KB-TPL-NOTIFY
title: Customer Notification Templates
document_type: script
department: customer_experience
sensitivity: internal
effective_from: 2025-03-01
status: current
---

# Customer Notification Templates

Use only these templates. Placeholders in braces are filled from the case record, never typed by hand.

- **credit_approved:** "Dear customer, ₹{amount} has been credited to your Kestrel account ending {last4} for your complaint {ticket_id}. Value date: {value_date}."
- **credit_rejected:** "Dear customer, your request {ticket_id} could not be approved: {reason}. You can escalate by replying ESCALATE or calling 1800-000-0000."
- **pending_approval:** "Dear customer, your request {ticket_id} is with our team for review. We will update you by {due_date}."
- **card_blocked:** "Your Kestrel card ending {last4} has been blocked as requested at {time}. Call us to order a replacement."
- **fee_reversed:** "Dear customer, the ₹{amount} fee on your Kestrel account ending {last4} has been reversed for your request {ticket_id}. Value date: {value_date}."
- **chargeback_raised:** "Dear customer, we have raised a dispute with the merchant for ₹{amount} on your card ending {last4} (reference {ticket_id}). Card networks usually give the merchant 30 to 45 days to respond; we will update you."
- **escalated:** "Your complaint {ticket_id} has been escalated to our Grievance Redressal Officer. Reference {ref}."
