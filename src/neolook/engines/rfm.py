"""
RFM (Recency, Frequency, Monetary) customer segmentation, plus a v1 naive
LTV estimate. Both work on the same orders_df shape analytics_engine.py
already produces (fetch_order_data) - no new Shopify queries needed.

RFM scores are quintiles (1-5) computed *relative to the current customer
base*, not fixed dollar/day thresholds - so "Champions" always means
"top ~20% of this store's customers," whether the store has 50 or 50,000
of them.

TODO (honest, not hidden - see docs/LTV_ROADMAP.md):
  - LTV here is a naive linear projection (AOV * order frequency * 12
    months), not a probabilistic model. It assumes each customer's
    observed purchase rate continues indefinitely, with no churn/dropout
    modeling - a customer who ordered twice in their first month looks
    identical to one who's been ordering steadily for a year.
  - RFM segment boundaries (Champions/Loyal/At-Risk/Hibernating/New/Others)
    are a common, simple heuristic over the 1-5 quintile scores, not
    tuned or validated against this store's actual churn behavior.
  - With fewer than 5 customers, quintile scoring degenerates (there's no
    meaningful way to split a handful of people into 5 equal buckets), so
    every customer gets a neutral score of 3 instead of a real quintile.
"""

from datetime import datetime, timezone
from typing import Any

import pandas as pd

MIN_CUSTOMERS_FOR_QUINTILES = 5

RFM_COLUMNS = [
    "customer_id", "customer_name", "recency_days", "frequency", "monetary",
    "r_score", "f_score", "m_score", "rfm_segment",
]


def _quintile_score(series: pd.Series, invert: bool = False) -> pd.Series:
    """1 (worst) - 5 (best) score, by quintile within `series`. invert=True
    means a *lower* raw value is better (used for recency: fewer days
    since the last order is better).

    Ties always get the same score (duplicates="drop") - e.g. many
    customers sharing the same order count is common in real data, and
    breaking that tie by arbitrary row order would let two customers with
    identical purchase history end up in different segments. When a value
    repeats often enough, this can mean fewer than 5 distinct scores are
    actually used - an honest reflection of the data, not a forced 5-way
    split."""
    values = -series if invert else series
    binned = pd.qcut(values, 5, duplicates="drop")
    return binned.cat.codes + 1


def _segment(r: int, f: int, m: int) -> str:
    """Simple, documented heuristic over the 1-5 R/F/M scores - see the
    module docstring's TODO about this not being a tuned/validated model."""
    if r >= 4 and f >= 4 and m >= 4:
        return "Champions"
    if f >= 4 or (r >= 3 and f >= 3):
        return "Loyal"
    if r <= 2 and f >= 3:
        return "At-Risk"
    if r >= 4 and f <= 1:
        return "New"
    if r <= 2 and f <= 2:
        return "Hibernating"
    return "Others"


def compute_rfm(orders_df: pd.DataFrame, reference_date: datetime | None = None) -> pd.DataFrame:
    """One row per customer: recency_days, frequency, monetary, their 1-5
    quintile scores, and the resulting segment label. Empty DataFrame in,
    empty (but correctly-columned) DataFrame out."""
    if orders_df.empty:
        return pd.DataFrame(columns=RFM_COLUMNS)

    df = orders_df.dropna(subset=["customer_id"])
    if df.empty:
        return pd.DataFrame(columns=RFM_COLUMNS)

    reference_date = reference_date or datetime.now(timezone.utc)

    per_customer = df.groupby("customer_id").agg(
        customer_name=("customer_name", "first"),
        last_order_date=("date", "max"),
        frequency=("order_id", "count"),
        monetary=("total_price", "sum"),
    )
    per_customer["recency_days"] = (reference_date - per_customer["last_order_date"]).dt.days

    if len(per_customer) < MIN_CUSTOMERS_FOR_QUINTILES:
        per_customer["r_score"] = 3
        per_customer["f_score"] = 3
        per_customer["m_score"] = 3
    else:
        per_customer["r_score"] = _quintile_score(per_customer["recency_days"], invert=True)
        per_customer["f_score"] = _quintile_score(per_customer["frequency"])
        per_customer["m_score"] = _quintile_score(per_customer["monetary"])

    per_customer["rfm_segment"] = [
        _segment(r, f, m)
        for r, f, m in zip(per_customer["r_score"], per_customer["f_score"], per_customer["m_score"])
    ]
    per_customer["monetary"] = per_customer["monetary"].round(2)

    return per_customer.reset_index()[RFM_COLUMNS]


def estimate_ltv_naive(customer_orders: pd.DataFrame, window_days: int) -> dict[str, Any]:
    """v1 naive LTV projection for one customer's orders within the lookback
    window: LTV = average_order_value * orders_per_month * 12. See the
    module docstring's TODO - this is a linear extrapolation of the
    observed purchase rate, not a churn-aware probabilistic model."""
    order_count = len(customer_orders)
    total_spent = float(customer_orders["total_price"].sum())
    average_order_value = total_spent / order_count if order_count else 0.0
    orders_per_month = order_count / (window_days / 30.0) if window_days else 0.0
    naive_12_month_ltv = average_order_value * orders_per_month * 12

    return {
        "orders_in_window": order_count,
        "average_order_value": round(average_order_value, 2),
        "orders_per_month": round(orders_per_month, 3),
        "historical_total_spent": round(total_spent, 2),
        "naive_projected_12_month_ltv": round(naive_12_month_ltv, 2),
        "methodology": "naive_v1_linear_projection",
    }
