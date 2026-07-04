# LTV / RFM roadmap

`feature/ltv-rfm` adds two tools on top of the existing analytics engine:
`segment_customers` (RFM: Recency, Frequency, Monetary) and
`estimate_customer_ltv` (a v1 naive lifetime-value estimate). Both are
real, working, tested against live data - this doc is about what's
deliberately *not* built yet, and why, so the gap is documented rather
than discovered later.

## What v1 actually does

- **RFM segmentation** (`src/neolook/engines/rfm.py`): scores every
  customer 1-5 on recency, frequency, and monetary value, relative to
  quintiles of *this store's own* customer base - not fixed dollar/day
  thresholds. Those three scores map to a segment label (Champions,
  Loyal, At-Risk, Hibernating, New, Others) via a simple, documented
  heuristic.
- **Naive LTV** (`estimate_ltv_naive`): `average_order_value *
  orders_per_month * 12`. A linear projection of the customer's observed
  purchase rate over the lookback window, extended out 12 months.

## Known limitations (honest, not hidden)

1. **The naive LTV assumes purchasing never stops.** A customer who
   ordered twice in their first month looks identical to one who's been
   ordering steadily for a year - there's no concept of a customer
   eventually churning. Real LTV models exist specifically to avoid this:

   - **BG/NBD (Beta-Geometric/Negative Binomial Distribution)** models
     *when* a customer is likely to churn, based on the pattern of time
     between their past orders (long, growing gaps predict dropout).
   - **Gamma-Gamma** models *how much* a customer spends per transaction,
     independent of how often they buy.
   - Combined, they answer "how many more purchases will this customer
     make, and how much will each be worth, before they likely stop
     buying" - a materially different and more defensible number than v1's
     flat extrapolation.
   - This wasn't built for v1 because it needs a customer base large
     enough, and with enough repeat-purchase history, to fit those
     distributions meaningfully (typically hundreds of repeat customers
     with multiple orders each) - the seeded dev store's ~150 customers
     over a 120-day window is thin for that, and forcing a probabilistic
     model onto too little data would produce a *falsely precise* number,
     which is worse than an honestly-labeled naive one.

2. **RFM segment boundaries are a common heuristic, not a validated
   model.** The Champions/Loyal/At-Risk/Hibernating/New/Others rules
   (see `_segment()` in `rfm.py`) are the standard textbook RFM cutoffs,
   not thresholds tuned or backtested against this store's actual repeat-
   purchase or churn behavior. They're a reasonable starting point, not a
   claim of predictive accuracy.

3. **Quintile scoring degrades with too little data or too many ties.**
   With fewer than 5 customers, there's no meaningful way to split people
   into 5 buckets, so everyone gets a neutral score of 3 instead (see
   `MIN_CUSTOMERS_FOR_QUINTILES` in `rfm.py`). Separately, when a lot of
   customers share the exact same value (e.g. many with identical order
   counts - common in real stores), ties are always scored together
   rather than split arbitrarily, which can mean fewer than 5 distinct
   scores actually get used. Both are the data telling you it doesn't
   support finer precision, not a bug being papered over.

## Planned upgrade path

- Swap `estimate_ltv_naive` for a BG/NBD + Gamma-Gamma model (the
  `lifetimes` Python package implements both) once there's enough repeat-
  purchase history to fit it meaningfully - expose both the naive and
  probabilistic estimates side by side during the transition, so the
  naive number isn't silently replaced without a comparison point.
- Backtest the RFM segment boundaries against actual repeat-purchase
  outcomes in this store's data (e.g. do "At-Risk" customers actually
  churn at a higher rate than "Loyal" ones?) instead of relying on the
  textbook heuristic as-is.
- Churn prediction and multi-touch attribution (already listed on the
  main README's roadmap) both build naturally on top of the RFM
  segmentation once it exists.
