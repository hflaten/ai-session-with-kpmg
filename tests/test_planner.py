"""The order planner (app/inventory/planner.py): pure functions, made-up data, no database."""
from datetime import date, timedelta

import pandas as pd
import pytest

from app.inventory import planner
from app.inventory.planner import Forecast, Item, corrected_demand, forecast, plan_item, recommend, simulate

TODAY = date(2026, 11, 6)   # a Friday


def history(days: int = 500, units: float = 10, stockout_days=(), promo_days=(), closed_sundays=False):
    rows = []
    for i in range(days, 0, -1):
        d = TODAY - timedelta(days=i)
        if closed_sundays and d.weekday() == 6:
            continue
        out = d in stockout_days
        rows.append({"date": d, "units_sold": 3 if out else units * (1.5 if d in promo_days else 1),
                     "stockout": out, "on_promotion": d in promo_days})
    return pd.DataFrame(rows)


def item(**kw) -> Item:
    base = dict(sku="S1", location_id="L01", location_type="online", unit_cost=10, price=20, shelf_life=30,
                pack_size=6, lead_time=2, lead_time_sd=0, order_weekdays=[0, 2, 4], batches=[], open_orders=[],
                last_year_orders=[])
    return Item(**{**base, **kw})


def flat_forecast(per_day: float, days: int = 90) -> Forecast:
    return Forecast({TODAY + timedelta(days=i): per_day for i in range(days)}, 1.0, 0, 0, 0.1, 0)


# ---------- demand + forecast ----------

def test_stockout_day_gets_the_demand_of_the_days_around_it():
    out = TODAY - timedelta(days=100)
    demand = corrected_demand(history(stockout_days={out}))
    assert demand[out] == pytest.approx(10)          # sold 3, but demand was ~10
    assert demand[out - timedelta(days=1)] == 10      # normal days unchanged


def test_forecast_is_last_year_times_trend():
    sales = history()
    # last 8 weeks sold 20% more than the same weeks last year
    recent = sales["date"] >= TODAY - timedelta(weeks=8)
    sales.loc[recent, "units_sold"] = 12
    fc = forecast(corrected_demand(sales), 0, TODAY, 14, "online")
    assert fc.trend == pytest.approx(1.2)
    assert fc.daily[TODAY] == pytest.approx(10 * 1.2)


def test_stores_sell_nothing_on_sundays_and_holidays_online_always_open():
    demand = corrected_demand(history(closed_sundays=True))
    store = forecast(demand, 0, TODAY, 60, "store")
    online = forecast(corrected_demand(history()), 0, TODAY, 60, "online")
    sunday, christmas = date(2026, 11, 8), date(2026, 12, 25)
    assert store.daily[sunday] == 0 and store.daily[christmas] == 0
    assert online.daily[sunday] > 0 and online.daily[christmas] > 0
    assert store.daily[date(2026, 12, 24)] > 0       # last year's same weekday was Christmas Day: uses a week before


def test_planned_promotion_raises_and_last_years_promotion_is_removed():
    last_year_promo = {TODAY + timedelta(days=5) - timedelta(days=364)}
    sales = history(promo_days=last_year_promo)
    demand = corrected_demand(sales)
    planned = {TODAY + timedelta(days=10)}
    fc = forecast(demand, 0, TODAY, 20, "online", past_promo_days=last_year_promo, future_promo_days=planned, uplift=1.5)
    assert fc.daily[TODAY + timedelta(days=5)] == pytest.approx(10)    # 15 last year / 1.5
    assert fc.daily[TODAY + timedelta(days=10)] == pytest.approx(15)   # 10 x 1.5
    assert planner.promo_uplift(demand, sales.set_index("date")["on_promotion"]) == pytest.approx(1.5)


# ---------- simulation ----------

def test_fifo_expiry_waste_and_lost_sales():
    # 10 units expire after 2 days, 5 per day demand: day 0 and 1 sell 5 each, then nothing left
    it = item(batches=[(TODAY + timedelta(days=1), 10.0), (TODAY + timedelta(days=2), 4.0)])
    r = simulate(it, flat_forecast(5), TODAY, 4, [])
    assert r.waste_units == 0 and r.lost_units == pytest.approx(20 - 14)
    assert r.first_stockout == TODAY + timedelta(days=2)
    it = item(batches=[(TODAY + timedelta(days=1), 30.0)])
    assert simulate(it, flat_forecast(5), TODAY, 4, []).waste_units == pytest.approx(20)   # 30 - 2 days x 5


# ---------- recommendations ----------

def test_orders_only_on_order_days_in_whole_packs_and_prevent_stockouts():
    it = item(batches=[(TODAY + timedelta(days=60), 10.0)])
    fc = flat_forecast(5)
    orders, _ = recommend(it, fc, TODAY, 28)
    assert orders and all(date.fromisoformat(o["order_date"]).weekday() in (0, 2, 4) for o in orders)
    assert all(o["qty"] % 6 == 0 for o in orders)
    with_orders = simulate(it, fc, TODAY, 28, [(date.fromisoformat(o["arrival_date"]), o["qty"]) for o in orders])
    assert with_orders.lost_units < 15        # only before the first delivery can arrive (stock lasts 2 days)
    assert simulate(it, fc, TODAY, 28, []).lost_units > 100


def test_perishables_are_not_ordered_beyond_shelf_life():
    it = item(shelf_life=2, pack_size=1, order_weekdays=[0, 1, 2, 3, 4, 5, 6], lead_time=1)
    orders, _ = recommend(it, flat_forecast(5), TODAY, 14)
    assert max(o["qty"] for o in orders) <= 10     # at most 2 days of demand


def test_no_holiday_orders():
    it = item(order_weekdays=[4], batches=[], lead_time=1)    # Fridays only; 25 Dec 2026 is a Friday
    orders, _ = recommend(it, flat_forecast(5, 120), TODAY, 60)
    assert "2026-12-25" not in [o["order_date"] for o in orders]


def test_overstock_flag_and_releasable_capital():
    it = item(batches=[(TODAY + timedelta(days=400), 600.0)])     # 600 units, 5 per day = 120 days
    row = plan_item(it, flat_forecast(5), TODAY, 56)
    assert row["overstock"] and row["action"] == "reduce" and row["days_of_cover"] == 120
    assert row["capital_releasable_nok"] == (600 - 5 * 42) * 10
    assert row["recommended_orders"] == []


def test_with_vs_without_business_as_usual():
    # business as usual: one late, too-small order; the recommendation avoids the lost sales
    it = item(last_year_orders=[(TODAY + timedelta(days=14), TODAY + timedelta(days=16), 12.0)])
    row = plan_item(it, flat_forecast(5), TODAY, 28)
    assert row["without_recommendation"]["lost_sales_nok"] > row["with_recommendation"]["lost_sales_nok"]
    assert row["nok_impact"] > 0
    assert row["inputs"]["business_as_usual_orders"] == 1
