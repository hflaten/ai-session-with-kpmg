"""Dashboard endpoints for the inventory case (no LLM: plain SQL + the deterministic planner).

GET  /insights/summary          KPI cards: stock value, waste last 90 days, stockout rate, capital in overstock
GET  /insights/recommendations  top recommendations by NOK impact + the overstock list, each with its inputs
POST /orders                    a human approves or rejects a recommended order (-> proposed_orders)
GET  /orders                    decided orders, newest first
"""
from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app import db
from app.tools.inventory import latest_decisions, plan, save_order, totals

router = APIRouter()

WASTE_DAYS = 90


def _today(rows_plan: dict) -> date:
    return date.fromisoformat(rows_plan["today"])


@router.get("/insights/summary")
def summary(horizon_days: int = 56):
    p = plan(horizon_days=horizon_days)
    today = _today(p)
    since = today - timedelta(days=WASTE_DAYS)
    with db.connect(readonly=True) as conn:
        stock = conn.execute("select coalesce(sum(stock_value_nok), 0) as value, coalesce(sum(units_on_hand), 0) as units "
                             "from inventory_current").fetchone()
        waste = conn.execute("select coalesce(sum(cost_nok), 0) as value, coalesce(sum(units), 0) as units "
                             "from waste where date >= %s", (since,)).fetchone()
        stockouts = conn.execute("select count(*) filter (where stockout) as stockout_days, count(*) as days "
                                 "from sales_daily where date >= %s", (since,)).fetchone()
    over = [r for r in p["rows"] if r["overstock"]]
    over_value = sum(r["inputs"]["on_hand_units"] * r["inputs"]["unit_cost_nok"] for r in over)
    rate = stockouts["stockout_days"] / stockouts["days"] if stockouts["days"] else 0.0
    return {
        "today": p["today"],
        "kpis": {
            "total_stock_value_nok": {
                "value": round(float(stock["value"])),
                "definition": "Stock on hand now, at purchase price (inventory_current.stock_value_nok).",
                "inputs": {"units_on_hand": int(stock["units"]), "items": len(p["rows"])}},
            "waste_last_90_days_nok": {
                "value": round(float(waste["value"])),
                "definition": f"Written-off stock at purchase price since {since.isoformat()} (waste.cost_nok).",
                "inputs": {"units": int(waste["units"]), "since": since.isoformat()}},
            "stockout_rate_last_90_days": {
                "value": round(rate, 4),
                "definition": ("Share of product-days (sku x location x open day) that sold out, last 90 days. "
                               "Fraction: 0.04 = 4%."),
                "inputs": {"stockout_days": stockouts["stockout_days"], "product_days": stockouts["days"],
                           "since": since.isoformat()}},
            "capital_in_overstock_nok": {
                "value": round(over_value),
                "definition": "Stock value of items with more than 6 weeks of cover (forecast demand).",
                "inputs": {"overstock_items": len(over),
                           "capital_releasable_nok": sum(r["capital_releasable_nok"] for r in over)}},
        },
        "plan_totals": {"horizon_days": p["horizon_days"], **totals(p["rows"])},
    }


@router.get("/insights/recommendations")
def recommendations(location_id: str | None = None, sku: str | None = None, limit: int = 30, horizon_days: int = 56):
    p = plan(location_id, sku, horizon_days)
    decisions = latest_decisions()

    def with_decision(row: dict) -> dict:
        nxt = row["next_order"]
        decision = decisions.get((row["sku"], row["location_id"], nxt["order_date"])) if nxt else None
        return {**row, "decision": decision}   # copy: the planner result is cached

    rows = sorted(p["rows"], key=lambda r: r["nok_impact"], reverse=True)
    overstock = sorted((r for r in rows if r["overstock"]), key=lambda r: r["capital_releasable_nok"], reverse=True)
    return {
        "today": p["today"], "horizon_days": p["horizon_days"],
        "recommendations": [with_decision(r) for r in rows[:max(1, min(limit, 200))]],
        "overstock": [with_decision(r) for r in overstock],
    }


class OrderDecision(BaseModel):
    sku: str
    location_id: str
    qty: int = Field(gt=0)
    order_date: date
    decision: Literal["approved", "rejected"]
    reason: str | None = None
    decided_by: str | None = None


@router.post("/orders")
def decide_order(body: OrderDecision):
    try:
        row = save_order(body.sku, body.location_id, body.qty, body.order_date.isoformat(), body.reason,
                         status=body.decision, source="dashboard", decided_by=body.decided_by)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"order": row}


@router.get("/orders")
def list_orders(limit: int = 100):
    with db.connect() as conn:
        rows = conn.execute(
            "select * from proposed_orders order by created_at desc limit %s", (max(1, min(limit, 500)),)).fetchall()
    return {"orders": rows}
