"""Live: dashboard endpoints against the uploaded case data (skipped if it isn't loaded)."""
import json

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.main import app

client = TestClient(app)
H = {"X-API-Key": "test-key"}


def _case_loaded() -> bool:
    if not settings.readonly_db_url:
        return False
    try:
        with db.connect(readonly=True) as conn:
            return conn.execute("select to_regclass('public.sales_daily') is not null as ok").fetchone()["ok"]
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _case_loaded(), reason="inventory case data not loaded")


def test_summary_has_the_four_kpis_with_definitions():
    k = client.get("/insights/summary", headers=H).json()["kpis"]
    assert set(k) == {"total_stock_value_nok", "waste_last_90_days_nok", "stockout_rate_last_90_days",
                      "capital_in_overstock_nok"}
    assert all("definition" in v and "inputs" in v for v in k.values())
    assert 0 <= k["stockout_rate_last_90_days"]["value"] <= 1


def test_recommendations_are_sorted_by_impact_with_inputs():
    body = client.get("/insights/recommendations", headers=H, params={"limit": 30}).json()
    rows = body["recommendations"]
    assert len(rows) == 30
    assert [r["nok_impact"] for r in rows] == sorted((r["nok_impact"] for r in rows), reverse=True)
    assert {"on_hand_units", "trend_factor", "forecast_weekly_units", "lead_time_days_avg"} <= set(rows[0]["inputs"])
    assert all(r["overstock"] for r in body["overstock"])
    json.dumps(body)    # plain JSON, no numpy types


def test_order_decision_is_saved_and_shown_on_the_recommendation():
    rec = next(r for r in client.get("/insights/recommendations", headers=H).json()["recommendations"] if r["next_order"])
    o = rec["next_order"]
    body = {"sku": rec["sku"], "location_id": rec["location_id"], "qty": o["qty"], "order_date": o["order_date"],
            "decision": "approved", "decided_by": "pytest"}
    saved = client.post("/orders", headers=H, json=body).json()["order"]
    try:
        assert saved["status"] == "approved" and saved["source"] == "dashboard"
        again = next(r for r in client.get("/insights/recommendations", headers=H).json()["recommendations"]
                     if (r["sku"], r["location_id"]) == (rec["sku"], rec["location_id"]))
        assert again["decision"]["id"] == saved["id"]
    finally:
        with db.connect() as conn:
            conn.execute("delete from proposed_orders where id = %s", (saved["id"],))


def test_unknown_sku_is_rejected():
    r = client.post("/orders", headers=H, json={"sku": "NOPE", "location_id": "L01", "qty": 1,
                                                "order_date": "2026-11-09", "decision": "approved"})
    assert r.status_code == 400
