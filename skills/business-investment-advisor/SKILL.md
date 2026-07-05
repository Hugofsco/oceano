---
name: business-investment-advisor
description: evaluate whether a purchase/hire/investment is worth the capital — ROI, payback, NPV, IRR, build-vs-buy, lease-vs-buy — use for "should I buy this", equipment/hardware purchases, or comparing where to put limited budget
status: published
notes: ported from claude-skills finance/business-investment-advisor (MIT); prose-only, no scripts — the formulas are simple enough for inline computation
---
# Business investment advisor

Business capital allocation, not personal stock/securities advice.

Get: the upfront cost, expected useful life or contract term, expected revenue increase
or cost savings per month/year, ongoing costs, and confidence level. Work with partial
data — state assumptions explicitly rather than blocking on missing numbers.

Formulas:
- **ROI** = (net gain / cost) × 100 — quick comparison, ignores time value of money
- **Payback period** = investment ÷ annual net cash flow — flag if payback exceeds the
  asset's useful life (never pays back) or exceeds ~3 years for a small purchase
- **NPV** = Σ [cashflow_t / (1+r)^t] − initial investment, r = cost of capital
  (8-15% typical for a small operation) — run this for anything >$25K or >12-month
  horizon; NPV<0 destroys value regardless of a positive-looking ROI
- **IRR** = the discount rate where NPV=0 — compare against a hurdle rate (10-15%
  stable, 20-25%+ growth/risky bet)

Always compute the downside case at 50% of projected revenue as a real decision input,
not an afterthought — optimistic projections are the default failure mode here. Always
ask what else the capital could do (opportunity cost) — debt paydown counts, its
guaranteed return is your interest rate.

Build vs buy: buy if a vendor does it ≥80% as well for <50% of the build cost.
Lease vs buy: buy when you'll use it past 60% of its useful life; lease when the
tech changes fast or cash preservation matters more than TCO.

Output: **RECOMMENDATION** (proceed / proceed with conditions / do not proceed) → the
numbers table (investment, payback, NPV, IRR) → assumptions (flag low-confidence ones) →
upside/downside case → risks → one concrete next step before committing capital.
