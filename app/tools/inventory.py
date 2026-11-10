"""Inventory tools for the agent.

recommend_orders       deterministic order plan (app/inventory/planner.py), no LLM involved
create_purchase_order  writes to proposed_orders; needs human approval first (like save_report)
"""
import time
from datetime import date, timedelta

from app import db
from app.inventory import data
from app.llm import Tool

TOP_ROWS_FOR_AGENT = 15      # unfiltered calls: the agent sees the top rows, not all 144
CACHE_SECONDS = 300          # the dashboard calls the planner twice per page load

_cache: dict[tuple, tuple[float, dict]] = {}


def plan(location_id: str | None = None, sku: str | None = None, horizon_days: int = 56) -> dict:
    """data.plan() with a 5-minute cache. clear_cache() is called after every table upload."""
    key = (location_id, sku, int(horizon_days))
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    result = data.plan(location_id, sku, horizon_days)
    _cache[key] = (time.monotonic(), result)
    return result


def clear_cache() -> None:
    _cache.clear()


def promo_periods(days: list[str]) -> list[dict]:
    """["2026-12-01", ..., "2026-12-10"] -> [{"start": "2026-12-01", "end": "2026-12-10"}]"""
    periods = []
    for d in sorted(days):
        if periods and date.fromisoformat(d) == date.fromisoformat(periods[-1]["end"]) + timedelta(days=1):
            periods[-1]["end"] = d
        else:
            periods.append({"start": d, "end": d})
    return periods


def compact(row: dict) -> dict:
    """What the agent needs from one planner row (the dashboard gets the full row with all inputs)."""
    i = row["inputs"]
    return {
        "sku": row["sku"], "product": row["product_name"], "location_id": row["location_id"],
        "location": row["location_name"], "supplier": row["supplier_name"], "action": row["action"],
        "days_of_cover": row["days_of_cover"], "stockout_risk_date": row["stockout_risk_date"],
        "expected_waste_nok": row["expected_waste_nok"], "next_order": row["next_order"],
        "orders_in_horizon": len(row["recommended_orders"]),
        "units_in_horizon": sum(o["qty"] for o in row["recommended_orders"]),
        "with_recommendation": row["with_recommendation"], "without_recommendation": row["without_recommendation"],
        "overstock": row["overstock"], "capital_releasable_nok": row["capital_releasable_nok"],
        "nok_impact": row["nok_impact"],
        "why": {"forecast_basis": "same days last year, stockout-corrected, x trend",
                "trend": i["trend_factor"], "last_8_weeks_units": i["last_8_weeks_units"],
                "same_8_weeks_last_year_units": i["same_8_weeks_last_year_units"],
                "forecast_weekly_units": i["forecast_weekly_units"], "forecast_daily_avg": i["forecast_daily_avg"],
                "on_hand": i["on_hand_units"], "on_order": i["on_order_units"],
                "lead_time_days": i["lead_time_days_avg"], "lead_time_sd_days": i["lead_time_days_sd"],
                "shelf_life_days": i["shelf_life_days"], "pack_size": i["pack_size"],
                "safety_stock_units": i["safety_stock_units"], "promo_uplift": i["promo_uplift"],
                "planned_promotions": promo_periods(i["planned_promo_days"])},
    }


def totals(rows: list[dict]) -> dict:
    def total(scenario: str, key: str) -> int:
        return sum(r[scenario][key] for r in rows)
    return {
        "with_recommendation": {k: total("with_recommendation", k) for k in ("capital_tied_nok", "lost_sales_nok", "waste_nok")},
        "without_recommendation": {k: total("without_recommendation", k) for k in ("capital_tied_nok", "lost_sales_nok", "waste_nok")},
        "overstock_items": sum(r["overstock"] for r in rows),
        "capital_releasable_nok": sum(r["capital_releasable_nok"] for r in rows),
    }


def recommend_orders(location_id: str | None = None, sku: str | None = None, horizon_days: int = 56) -> dict:
    result = plan(location_id, sku, horizon_days)
    rows = sorted(result["rows"], key=lambda r: r["nok_impact"], reverse=True)
    filtered = bool(location_id or sku)
    shown = rows if filtered else rows[:TOP_ROWS_FOR_AGENT]
    return {
        "today": result["today"], "horizon_days": result["horizon_days"],
        "method": ("Forecast = same days last year (lost sales on stockout days added back, promotions adjusted) "
                   "x trend (last 8 weeks vs same weeks last year). 'without' = business as usual (open orders + "
                   "the same orders as the same weeks last year); 'with' = the recommended orders. Capital at "
                   "purchase price (average over the horizon), lost sales at selling price, waste at purchase price. "
                   "nok_impact = lost sales avoided + waste avoided + capital releasable from overstock."),
        "totals": totals(rows),
        "rows_total": len(rows),
        "rows": [compact(r) for r in shown],
        "overstock": [{"sku": r["sku"], "product": r["product_name"], "location_id": r["location_id"],
                       "location": r["location_name"], "days_of_cover": r["days_of_cover"],
                       "stock_value_nok": round(r["inputs"]["on_hand_units"] * r["inputs"]["unit_cost_nok"]),
                       "capital_releasable_nok": r["capital_releasable_nok"]}
                      for r in sorted(rows, key=lambda r: r["capital_releasable_nok"], reverse=True) if r["overstock"]],
    }


recommend_orders_tool = Tool(
    name="recommend_orders",
    description=(
        "Deterministic order plan per product and location (calculated in Python, not guessed). "
        "Returns, per sku + location: days of cover, stockout risk date, expected waste from expiring batches, "
        "the next recommended order (date, qty) and all orders in the horizon, and the consequences with vs "
        "without the recommendation: capital tied up, lost sales and waste in NOK. Also lists overstock "
        "(more than 6 weeks of cover) with the capital that could be released. Without filters you get the "
        f"top {TOP_ROWS_FOR_AGENT} rows by NOK impact plus totals; filter by location_id and/or sku for details. "
        "Use this for any question about what or how much to order, stockouts, waste risk or overstock. "
        "The result has a call_id: cite a claim as a tool source with that call_id, the row "
        "({\"sku\": ..., \"location_id\": ...} or \"summary\" for totals) and the fields holding its numbers."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "location_id": {"type": "string", "description": "Optional, e.g. L01"},
            "sku": {"type": "string", "description": "Optional, e.g. SKU009"},
            "horizon_days": {"type": "integer", "description": "Days ahead to plan (7-120, default 56)"},
        },
    },
    run=recommend_orders,
)


# ---------- create_purchase_order ----------

def save_order(sku: str, location_id: str, qty: int, order_date: str, reason: str | None,
               status: str, source: str, run_id: str | None = None, decided_by: str | None = None) -> dict:
    """Insert one decided order. Raises ValueError on bad input."""
    qty = int(qty)
    if qty <= 0:
        raise ValueError("qty must be a positive whole number")
    day = date.fromisoformat(str(order_date)[:10])
    with db.connect() as conn:
        known = conn.execute("select (select count(*) from products where sku = %s) as p, "
                             "(select count(*) from locations where location_id = %s) as l",
                             (sku, location_id)).fetchone()
        if not known["p"] or not known["l"]:
            raise ValueError(f"unknown sku '{sku}' or location_id '{location_id}'")
        return conn.execute(
            """insert into proposed_orders (run_id, sku, location_id, qty, order_date, reason, status, source, decided_by)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               returning id, created_at, sku, location_id, qty, order_date, reason, status, source, decided_by""",
            (run_id, sku, location_id, qty, day, reason, status, source, decided_by),
        ).fetchone()


def latest_decisions() -> dict[tuple, dict]:
    """{(sku, location_id, order_date as text): newest decision}, to show on the dashboard."""
    with db.connect() as conn:
        rows = conn.execute(
            """select distinct on (sku, location_id, order_date)
                      id, created_at, sku, location_id, qty, order_date, status, source, decided_by
               from proposed_orders order by sku, location_id, order_date, created_at desc""").fetchall()
    return {(r["sku"], r["location_id"], r["order_date"].isoformat()): r for r in rows}


def make_create_purchase_order_tool(run_id: str | None) -> Tool:
    def create_purchase_order(sku: str, location_id: str, qty: int, order_date: str, reason: str) -> dict:
        row = save_order(sku, location_id, qty, order_date, reason, status="approved", source="agent", run_id=run_id)
        return {"created": True, "order_id": str(row["id"]), "sku": sku, "location_id": location_id,
                "qty": row["qty"], "order_date": row["order_date"].isoformat()}

    return Tool(
        name="create_purchase_order",
        description=("Create a purchase order proposal for one product at one location. A human must approve it "
                     "before it is saved, so only call it when the user asks to place or create orders. Use the "
                     "quantity and order date from recommend_orders and give a short reason."),
        input_schema={
            "type": "object",
            "properties": {
                "sku": {"type": "string", "description": "e.g. SKU009"},
                "location_id": {"type": "string", "description": "e.g. L01"},
                "qty": {"type": "integer", "description": "Units to order (whole packs)"},
                "order_date": {"type": "string", "description": "YYYY-MM-DD"},
                "reason": {"type": "string", "description": "Why, in one sentence"},
            },
            "required": ["sku", "location_id", "qty", "order_date", "reason"],
        },
        run=create_purchase_order,
        requires_approval=True,
    )
