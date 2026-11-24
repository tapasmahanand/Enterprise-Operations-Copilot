# RBI sources and figures

Every regulatory figure used in `rules/kestrel_rules.yaml`, the synthetic policy corpus and the calculator is listed here with its source. Checked 2026-11-24.

Kestrel Bank is fictional. Its policies are written to be *aligned with* these directions; they are not RBI text.

## R17 — Limiting liability of customers in unauthorised electronic banking transactions (2017)

- **Source:** RBI/2017-18/15, DBR.No.Leg.BC.78/09.07.005/2017-18, 6 July 2017. https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=11040
- **Applies in this project to:** transactions dated **before 1 January 2027** (see R26).

| Rule id | Figure | Para |
|---|---|---|
| R17-6i | Zero liability where the bank was negligent or deficient, whether or not reported | 6(i) |
| R17-6ii | Zero liability for third-party breach if the customer notifies **within 3 working days of receiving the bank's communication** | 6(ii) |
| R17-7i | Customer negligence: customer bears the whole loss until reported; bank bears loss after reporting | 7(i) |
| R17-7ii | Third-party breach reported in **4–7 working days**: liability = lower of transaction value and Table 1 cap | 7(ii) |
| R17-T1 | Table 1 caps: BSBD **₹5,000**; other savings, PPI/gift cards, MSME current/CC/OD, individual current/CC/OD with average balance or limit ≤ ₹25 lakh, credit cards with limit ≤ ₹5 lakh **₹10,000**; all other current/CC/OD and credit cards with limit > ₹5 lakh **₹25,000** | Table 1 |
| R17-8 | Beyond 7 working days: as per the bank's Board-approved policy | 8 |
| R17-WD | Working days counted per the home branch's working schedule, **excluding the date the communication was received** | Table 2 note |
| R17-9 | Shadow credit **within 10 working days** of the customer's notification, value-dated to the transaction date | 9 |
| R17-10 | Resolve and establish liability **within 90 days** of receiving the complaint | 10 |
| R17-12 | Burden of proving customer liability lies on the bank | 12 |

## R26 — Customer protection in fraudulent electronic banking transactions (2026 Directions, effective 2027)

- **Source:** Reserve Bank of India (Commercial Banks – Responsible Business Conduct) Third Amendment Directions, 2026. RBI/2026-27/167, DOR.MCS.REC.No.130/01-01-032/2026-27, issued 24 June 2026, **applies to transactions on or after 1 January 2027**. Replaces the R17-derived paragraphs (Section D, paras 64–76 of the 2025 RBC Directions) with Section DA. https://www.rbi.org.in/Scripts/NotificationUser.aspx?Id=13543

| Rule id | Figure | Para |
|---|---|---|
| R26-76K | Bank bears burden of proof and must classify each complaint | 76K |
| R26-76L | Zero liability for bank negligence, irrespective of reporting | 76L |
| R26-76M | Zero liability for third-party breach if reported **within 5 calendar days of occurrence**; later reports per bank policy | 76M |
| R26-76N | Customer negligence: liable until reported (less any 76T compensation) | 76N |
| R26-76O | Losses after reporting borne by the bank | 76O |
| R26-76Q | Resolution **≤ 45 calendar days** (domestic) / **60** (cross-border) from receipt | 76Q |
| R26-76R | Reversal value-dated; **credit cards: shadow reversal within 5 calendar days** of notification | 76R |
| R26-76T | One-time (lifetime) compensation for individuals: gross loss ≤ **₹50,000**, pays **lower of 85% of net loss and ₹25,000**; must report to bank and NCRP/1930 **within 5 calendar days**; paid within 5 calendar days of application | 76T |
| R26-76D | Mandatory instant SMS alert for transactions above ₹500 | 76D |

**Not confirmed from the primary text:** a shadow-credit deadline for non-credit-card products under R26. Kestrel applies its own internal standard (KB-INT-SC: 10 working days) and the copilot labels it as bank policy, not RBI.

## R19 — Harmonised TAT and compensation for failed transactions (2019)

- **Source:** RBI/2019-20/67, DPSS.CO.PD No.629/02.01.014/2019-20, 20 Sept 2019, effective 15 Oct 2019. https://rbi.org.in/Scripts/NotificationUser.aspx?Id=11693
- T = transaction date (calendar date). Compensation is credited suo moto, without a complaint.

| Rule id | Channel / failure | Auto-reversal by | Compensation |
|---|---|---|---|
| R19-1a | ATM (incl. micro-ATM): debited, cash not dispensed | T+5 | ₹100/day beyond T+5 |
| R19-2a | Card-to-card: debited, beneficiary not credited | T+1 | ₹100/day beyond T+1 |
| R19-2b | PoS card-present: debited, no merchant confirmation | T+5 | ₹100/day beyond T+5 |
| R19-3a | IMPS: debited, beneficiary not credited | T+1 | ₹100/day beyond T+1 |
| R19-4a | UPI transfer: debited, beneficiary not credited | T+1 | ₹100/day beyond T+1 |
| R19-4b | UPI merchant: debited, no merchant confirmation | T+5 | ₹100/day beyond T+5 |

**Not confirmed:** row 2c (card-not-present / e-commerce) figures did not extract from the web page. The calculator refuses this channel until it is checked against the PDF.

## Integrated Ombudsman

RBI Integrated Ombudsman Scheme, 2021: a customer may approach the Ombudsman if the bank does not resolve the complaint satisfactorily (referenced in R19 para 6). Used only in the escalation SOP.
