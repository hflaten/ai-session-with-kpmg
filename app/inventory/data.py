"""Load the inventory case tables from Postgres and run the planner for every sku + location.

Reads (as agent_readonly): products, suppliers, locations, sales_daily,
inventory_batches_current, purchase_orders. "Today" = the day after the last sales date.
"""
from datetime import date, timedelta

import pandas as pd

from app import db
from app.inventory.planner import (OVERSTOCK_DAYS, STOCKOUT_WINDOW, TREND_WEEKS, YEAR, Item, corrected_demand,
                                   forecast, plan_item, promo_uplift)

EXTRA_FORECAST_DAYS = 21   # the last orders in the horizon need demand a bit beyond it


def _day(v) -> date:
    return pd.Timestamp(v).date()


def _query(conn, sql: str, params=()) -> pd.DataFrame:
    return pd.DataFrame(conn.execute(sql, params).fetchall())


def planned_promo_days(promotions: pd.DataFrame, sku: str, location_id: str) -> set[date]:
    days = set()
    for r in promotions.itertuples() if not promotions.empty else []:
        if r.sku == sku and (r.locations == "all" or location_id in str(r.locations).split(",")):
            day, end = _day(r.start_date), _day(r.end_date)
            while day <= end:
                days.add(day)
                day += timedelta(days=1)
    return days


def load(location_id: str | None = None, sku: str | None = None) -> dict:
    where, params = [], []
    if location_id:
        where.append("location_id = %s"); params.append(location_id)
    if sku:
        where.append("sku = %s"); params.append(sku)
    filt = (" and " + " and ".join(where)) if where else ""

    with db.connect(readonly=True) as conn:
        today = _day(conn.execute("select max(date) as d from sales_daily").fetchone()["d"]) + timedelta(days=1)
        history_start = today - timedelta(days=YEAR + 7 * TREND_WEEKS + STOCKOUT_WINDOW + 7)
        data = {
            "today": today,
            "products": _query(conn, "select * from products" + (" where sku = %s" if sku else ""), [sku] if sku else []),
            "suppliers": _query(conn, "select * from suppliers"),
            "locations": _query(conn, "select * from locations"
                                + (" where location_id = %s" if location_id else ""), [location_id] if location_id else []),
            "sales": _query(conn, "select date, location_id, sku, units_sold, stockout, on_promotion from sales_daily "
                                  "where date >= %s" + filt, [history_start, *params]),
            "batches": _query(conn, "select location_id, sku, units, expiry_date from inventory_batches_current "
                                    "where true" + filt, params),
            "open_orders": _query(conn, "select location_id, sku, expected_date, qty_ordered from purchase_orders "
                                        "where status = 'åpen'" + filt, params),
            "promotions": _query(conn, "select sku, start_date, end_date, locations from promotions where end_date >= %s",
                                 [today]),
            # business as usual = the same orders as the same weeks last year
            "last_year_orders": _query(conn, "select location_id, sku, order_date, expected_date, qty_ordered "
                                             "from purchase_orders where order_date >= %s and order_date < %s" + filt,
                                       [today - timedelta(days=YEAR), today - timedelta(days=YEAR - 120), *params]),
        }
    return data


def plan(location_id: str | None = None, sku: str | None = None, horizon_days: int = 56) -> dict:
    """Run the planner for every matching sku + location. Rows have names attached."""
    horizon_days = max(7, min(int(horizon_days), 120))
    d = load(location_id, sku)
    today = d["today"]
    if d["products"].empty or d["locations"].empty:
        return {"today": today.isoformat(), "horizon_days": horizon_days, "rows": []}

    suppliers = d["suppliers"].set_index("supplier_id")
    groups = {k: g for k, g in d["sales"].groupby(["location_id", "sku"])} if not d["sales"].empty else {}

    def rows_for(frame: pd.DataFrame, loc: str, s: str) -> pd.DataFrame:
        if frame.empty:
            return frame
        return frame[(frame["location_id"] == loc) & (frame["sku"] == s)]

    rows = []
    for _, loc in d["locations"].iterrows():
        for _, p in d["products"].iterrows():
            sales = groups.get((loc["location_id"], p["sku"]))
            if sales is None:
                continue
            sales = sales.assign(date=sales["date"].map(_day))
            sup = suppliers.loc[p["supplier_id"]]
            shift = timedelta(days=YEAR)
            item = Item(
                sku=p["sku"], location_id=loc["location_id"], location_type=loc["type"],
                unit_cost=float(p["unit_cost_nok"]), price=float(p["price_nok"]),
                shelf_life=int(p["shelf_life_days"]), pack_size=int(p["pack_size"]),
                lead_time=float(sup["lead_time_days_avg"]), lead_time_sd=float(sup["lead_time_days_sd"]),
                order_weekdays=[int(x) for x in str(sup["order_weekdays"]).split(",") if x.strip() != ""],
                batches=[(_day(r.expiry_date), float(r.units)) for r in rows_for(d["batches"], loc["location_id"], p["sku"]).itertuples()],
                open_orders=[(_day(r.expected_date), float(r.qty_ordered))
                             for r in rows_for(d["open_orders"], loc["location_id"], p["sku"]).itertuples()],
                last_year_orders=[(_day(r.order_date) + shift, _day(r.expected_date) + shift, float(r.qty_ordered))
                                  for r in rows_for(d["last_year_orders"], loc["location_id"], p["sku"]).itertuples()
                                  if _day(r.order_date) + shift < today + timedelta(days=horizon_days)],
            )
            demand = corrected_demand(sales)
            history = sales.sort_values("date").set_index("date")
            fc = forecast(demand, int(sales["stockout"].sum()), today, horizon_days + EXTRA_FORECAST_DAYS, loc["type"],
                          past_promo_days=set(history.index[history["on_promotion"].astype(bool)]),
                          future_promo_days=planned_promo_days(d["promotions"], p["sku"], loc["location_id"]),
                          uplift=promo_uplift(demand, history["on_promotion"]))
            row = plan_item(item, fc, today, horizon_days)
            row.update({"product_name": p["name"], "category": p["category"], "location_name": loc["name"],
                        "supplier_id": p["supplier_id"], "supplier_name": sup["name"]})
            rows.append(row)
    return {"today": today.isoformat(), "horizon_days": horizon_days, "overstock_threshold_days": OVERSTOCK_DAYS,
            "rows": rows}
