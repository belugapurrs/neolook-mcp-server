"""
Unit tests for the RFM segmentation + naive LTV engine (pure pandas
functions, no network calls - see tests/test_analytics_tools.py for the
MCP tool wrappers).
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from neolook.engines import rfm


def _order(customer_id: str, customer_name: str, days_ago: float, price: float, order_id: str | None = None) -> dict:
    return {
        "order_id": order_id or f"order-{customer_id}-{days_ago}-{price}",
        "customer_id": customer_id,
        "customer_name": customer_name,
        "date": datetime.now(timezone.utc) - timedelta(days=days_ago),
        "total_price": price,
    }


def test_compute_rfm_labels_champions_and_hibernating():
    rows = []

    # 18 "average" filler customers with a realistic spread of recency,
    # frequency (1-4 orders), and price - enough distinct values for
    # quintile scoring to actually form 5 real buckets, unlike a small
    # hand-picked cluster where every value would be exactly tied.
    for i in range(18):
        cid = f"filler-{i}"
        frequency = 1 + (i % 4)
        recency_days = 30 + i * 10
        price = 30.0 + i * 4
        for order_num in range(frequency):
            rows.append(_order(cid, cid, recency_days + order_num * 3, price))

    # One clear "champion": most recent, most frequent (8 orders - above
    # every filler's 1-4), highest total spend by a wide margin.
    for order_num in range(8):
        rows.append(_order("champion", "Champion", 1 + order_num, 300.0))

    # One clear "hibernator": oldest last order, a single small purchase.
    rows.append(_order("hibernator", "Hibernator", 350, 5.0))

    result = rfm.compute_rfm(pd.DataFrame(rows))

    champ = result[result["customer_id"] == "champion"].iloc[0]
    hib = result[result["customer_id"] == "hibernator"].iloc[0]
    assert champ["rfm_segment"] == "Champions"
    assert hib["rfm_segment"] == "Hibernating"


def test_compute_rfm_empty_orders_returns_empty_frame_with_columns():
    result = rfm.compute_rfm(pd.DataFrame())
    assert result.empty
    assert list(result.columns) == rfm.RFM_COLUMNS


def test_compute_rfm_small_customer_base_uses_neutral_scores():
    rows = [_order("c1", "Ada", 1, 100.0), _order("c2", "Bea", 2, 50.0)]
    result = rfm.compute_rfm(pd.DataFrame(rows))
    assert (result["r_score"] == 3).all()
    assert (result["f_score"] == 3).all()
    assert (result["m_score"] == 3).all()


def test_estimate_ltv_naive():
    rows = [_order("c1", "Ada", 1, 100.0), _order("c1", "Ada", 15, 100.0)]
    result = rfm.estimate_ltv_naive(pd.DataFrame(rows), window_days=30)
    assert result["orders_in_window"] == 2
    assert result["average_order_value"] == 100.0
    assert result["orders_per_month"] == 2.0
    assert result["naive_projected_12_month_ltv"] == 2400.0
    assert result["methodology"] == "naive_v1_linear_projection"


@pytest.mark.skip(reason="WIP - probabilistic LTV (BG/NBD + Gamma-Gamma) not implemented yet, see docs/LTV_ROADMAP.md")
def test_estimate_ltv_probabilistic_bgnbd_gamma_gamma():
    """Placeholder for the v2 probabilistic LTV model described in
    docs/LTV_ROADMAP.md - it should account for purchase-timing variance
    and per-customer dropout/churn probability, instead of v1's flat linear
    projection of the observed purchase rate."""
    raise NotImplementedError
