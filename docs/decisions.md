# Decision log

One entry per non-obvious choice: what we chose, the main alternative, and why. Kept short on purpose; it feeds interview prep.

| # | Date | Decision | Alternative considered | Why |
|---|---|---|---|---|
| D1 | 2026-11-23 | Mixed graph: only Supervisor, Retrieval (query rewrite) and Composer call an LLM; Compliance, Approval, Memory, Tool Execution are deterministic code | Seven LLM agents (literal brief); one ReAct agent | Agent on a live call needs answers in seconds (2–3 LLM calls vs 6–9); deterministic guards are testable; still 7 units with contracts. ReAct fails the multi-agent criterion. |
| D2 | 2026-11-23 | Two graphs: a chat graph that never pauses, and a per-case graph that pauses at approval | Pause inside the chat run | Approval takes hours; the agent needs the answer and script while the customer is on hold. |
| D3 | 2026-11-23 | All refunds/credits need approval; matrix decides approver tier, other risky actions and refusals | Amount threshold only | Tapas's rule; matrix keeps the gate's logic non-trivial and testable. |
| D4 | 2026-11-23 | Copilot proposes actions; agent confirms with "Send to supervisor"; amount always from transaction lookup | Draft only on explicit request; amount from customer's words | Less agent effort, human stays in charge; a customer-stated amount can't drive an approval tier. |
| D5 | 2026-11-24 | LLM never does date/₹ arithmetic: a pure-function calculator returns figures plus rule ids | Let the Composer reason over policy text | Arithmetic over working-day calendars is where LLMs fail silently; rule ids make answers auditable. |
| D6 | 2026-11-24 | Implement both RBI liability regimes, selected by transaction date (R17 before 1 Jan 2027, R26 on/after) | Only one regime | The 2026 Directions take effect 1 Jan 2027, during the project's life; effective-dating is how banks handle rule changes. *(Recommended; awaiting Tapas's pick.)* |
| D7 | 2026-11-24 | One YAML rules file is the source of truth for the matrix, calculator and TAT table; every RBI figure traced in `docs/rbi-sources.md` | Hard-code figures in Python; derive rules from documents with an LLM | Reviewable diff when a rule changes; no LLM in the loop for regulatory numbers. |
| D8 | 2026-11-24 | Calculator refuses unconfirmed inputs (card-not-present TAT) instead of guessing | Use a plausible default | A wrong regulatory figure is worse than a refusal in a bank. |
| D9 | 2026-11-24 | Working-day calendar: Sundays, 2nd/4th Saturdays and fixed national holidays only (illustrative home branch) | Full state-wise holiday list | Real lists vary by state and year; the mechanism is what's being shown. Stated as a limitation. |
