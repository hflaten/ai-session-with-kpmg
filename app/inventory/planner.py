"""Order recommendations: deterministic Python/pandas, no LLM.

For every product (sku) at every location:

1. Demand history, corrected for lost sales. On a stockout day units_sold is lower than
   demand, so we estimate that day's demand from the in-stock days around it (+-10 days),
   adjusted for the weekday:  estimate = level x weekday_index.
2. Forecast per day: the same day last year (364 days back = same weekday), times a trend
   factor = last 8 weeks / the same 8 weeks last year. Stores sell nothing on Sundays and
   public holidays; the online shop is always open. Promotions: last year's promotion days
   are divided by the product's promotion uplift, planned promotions multiplied by it.
3. Simulate stock day by day (FIFO by expiry date): deliveries arrive, expired batches
   become waste, demand is sold from the batch that expires first, the rest is lost sales.
   Three scenarios:
     - no_new_orders:    current stock + open purchase orders only -> days of cover,
                         stockout risk date, waste from batches expiring now
     - business_as_usual ("without"): + the same orders as the same weeks last year
     - recommended       ("with"):    + the orders this module recommends
4. Recommended orders: on each day the supplier takes orders (not on public holidays),
   simulate ahead without a new order until the NEXT possible delivery; order the units we
   would be short in that window, plus safety stock (95% service level, demand and lead-time
   variation), but never more than can be sold before it expires. Rounded up to whole packs.
5. Overstock: more than 6 weeks of cover; the capital above 6 weeks could be released.

All money in NOK. Stock value and waste at purchase price, lost sales at selling price.
"""
import math
from functools import lru_cache
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd
from dateutil.easter import easter

YEAR = 364                 # days back to the same weekday last year
TREND_WEEKS = 8
TREND_MIN, TREND_MAX = 0.5, 2.5
TREND_MIN_UNITS = 20       # below this many units last year, the trend is set to 1.0
STOCKOUT_WINDOW = 10       # days on each side used to estimate demand on a stockout day
SERVICE_Z = 1.65           # safety stock for ~95% service level
OVERSTOCK_DAYS = 42        # more than 6 weeks of cover = overstock


# ---------- calendar ----------

@lru_cache(maxsize=None)
def norwegian_holidays(year: int) -> frozenset[date]:
    e = easter(year)
    fixed = {date(year, 1, 1), date(year, 5, 1), date(year, 5, 17), date(year, 12, 25), date(year, 12, 26)}
    moving = {e + timedelta(days=d) for d in (-3, -2, 0, 1, 39, 49, 50)}  # Maundy Thu ... Whit Monday
    return frozenset(fixed | moving)


def is_open(day: date, location_type: str) -> bool:
    if location_type == "online":
        return True
    return day.weekday() != 6 and day not in norwegian_holidays(day.year)


# ---------- 1. demand history corrected for stockouts ----------

def corrected_demand(sales: pd.DataFrame) -> pd.Series:
    """sales for ONE sku+location: date, units_sold, stockout. Returns demand per date (open days only).

    Days without a row are closed days (no demand)."""
    s = sales.sort_values("date").set_index("date")
    units = s["units_sold"].astype(float)
    in_stock = ~s["stockout"].astype(bool)
    weekday = pd.Series([d.weekday() for d in s.index], index=s.index)

    overall = units[in_stock].mean()
    by_weekday = units[in_stock].groupby(weekday[in_stock]).mean()
    index = (by_weekday / overall).to_dict() if overall and overall > 0 else {}

    demand = units.copy()
    for day in s.index[~in_stock]:
        near = (s.index >= day - timedelta(days=STOCKOUT_WINDOW)) & (s.index <= day + timedelta(days=STOCKOUT_WINDOW))
        ok = near & in_stock.values
        weights = sum(index.get(w, 1.0) for w in weekday[ok])
        if weights > 0:
            level = units[ok].sum() / weights
            demand[day] = max(units[day], level * index.get(day.weekday(), 1.0))
    return demand


# ---------- 2. forecast ----------

@dataclass
class Forecast:
    daily: dict[date, float]             # future day -> expected demand
    trend: float
    recent_units: float                  # last 8 weeks (corrected)
    recent_last_year_units: float        # same 8 weeks last year (corrected)
    cv: float                            # week-to-week variation (std / mean) in the last 8 weeks
    stockout_days_used: int              # stockout days in the history we used
    promo_uplift: float = 1.0            # demand on promotion days / normal days
    planned_promo_days: list[str] = field(default_factory=list)


def promo_uplift(demand: pd.Series, on_promotion: pd.Series) -> float:
    """How much more sells on promotion days than on normal days around them (+-14 days). 1.0 if unknown."""
    promo = on_promotion.astype(bool)
    sold, normal = 0.0, 0.0
    for day in demand.index[promo.values]:
        near = (demand.index >= day - timedelta(days=14)) & (demand.index <= day + timedelta(days=14)) & ~promo.values
        if near.any():
            sold += demand[day]
            normal += demand[near].mean()
    return min(3.0, max(1.0, sold / normal)) if normal > 0 else 1.0


def forecast(demand: pd.Series, stockout_days: int, today: date, days: int, location_type: str,
             past_promo_days: set[date] = frozenset(), future_promo_days: set[date] = frozenset(),
             uplift: float = 1.0) -> Forecast:
    """past_promo_days: promotion days last year (their extra sales are removed from the base);
    future_promo_days: planned promotions (base x uplift)."""
    d = {k.date() if isinstance(k, pd.Timestamp) else k: float(v) for k, v in demand.items()}

    def total(start: date, end: date) -> float:  # [start, end)
        return sum(v for k, v in d.items() if start <= k < end)

    window = timedelta(weeks=TREND_WEEKS)
    recent = total(today - window, today)
    last_year = total(today - window - timedelta(days=YEAR), today - timedelta(days=YEAR))
    trend = 1.0 if last_year < TREND_MIN_UNITS else min(TREND_MAX, max(TREND_MIN, recent / last_year))

    weeks = [total(today - timedelta(weeks=w + 1), today - timedelta(weeks=w)) for w in range(TREND_WEEKS)]
    cv = float(np.std(weeks) / np.mean(weeks)) if np.mean(weeks) > 0 else 0.5

    daily = {}
    for i in range(days):
        day = today + timedelta(days=i)
        if not is_open(day, location_type):
            daily[day] = 0.0
            continue
        same_day = day - timedelta(days=YEAR)
        if same_day in d:
            base = d[same_day]
        else:  # that day was closed last year (e.g. Christmas Day): use the same weekday a week before/after
            near = [d[x] for x in (same_day - timedelta(days=7), same_day + timedelta(days=7)) if x in d]
            base = sum(near) / len(near) if near else 0.0
        if same_day in past_promo_days:      # last year's promotion won't repeat
            base /= uplift
        if day in future_promo_days:         # a planned promotion this year
            base *= uplift
        daily[day] = base * trend
    return Forecast(daily, round(float(trend), 3), float(recent), float(last_year), round(cv, 3), stockout_days,
                    round(float(uplift), 2), sorted(x.isoformat() for x in future_promo_days if today <= x < today + timedelta(days=days)))


# ---------- 3. stock simulation ----------

@dataclass
class Result:
    lost_units: float = 0.0
    waste_units: float = 0.0
    stock_value_days: list[float] = field(default_factory=list)   # end-of-day stock value
    first_stockout: date | None = None


def _step(day: date, batches: list[list], arrivals: dict[date, list], demand: float,
          shelf_life: int, result: Result | None) -> None:
    """One day: receive, throw away expired, sell FIFO. batches = [[expiry_date, units], ...]."""
    for units in arrivals.get(day, []):
        batches.append([day + timedelta(days=shelf_life), units])
    batches.sort(key=lambda b: b[0])
    expired = sum(u for e, u in batches if e < day)
    batches[:] = [b for b in batches if b[0] >= day]
    left = demand
    for b in batches:
        take = min(b[1], left)
        b[1] -= take
        left -= take
    batches[:] = [b for b in batches if b[1] > 1e-9]
    if result is not None:
        result.waste_units += expired
        result.lost_units += left
        if left >= 0.5 and result.first_stockout is None:
            result.first_stockout = day


@dataclass
class Item:
    """Everything the planner needs for one sku at one location."""
    sku: str
    location_id: str
    location_type: str
    unit_cost: float
    price: float
    shelf_life: int
    pack_size: int
    lead_time: float
    lead_time_sd: float
    order_weekdays: list[int]
    batches: list[tuple[date, float]]               # (expiry_date, units) in stock now
    open_orders: list[tuple[date, float]]           # (expected_date, units)
    last_year_orders: list[tuple[date, date, float]]  # (order_date, expected_date, units), shifted to this year


def simulate(item: Item, fc: Forecast, today: date, days: int, extra_orders: list[tuple[date, float]]) -> Result:
    """extra_orders: (arrival_date, units) on top of the open orders."""
    batches = [[e, u] for e, u in item.batches]
    arrivals: dict[date, list] = {}
    for when, units in [*item.open_orders, *extra_orders]:
        arrivals.setdefault(max(when, today), []).append(units)   # late orders: assume they arrive today
    result = Result()
    for i in range(days):
        day = today + timedelta(days=i)
        _step(day, batches, arrivals, fc.daily.get(day, 0.0), item.shelf_life, result)
        result.stock_value_days.append(sum(u for _, u in batches) * item.unit_cost)
    return result


def recommend(item: Item, fc: Forecast, today: date, days: int) -> tuple[list[dict], float]:
    """Order-up-to policy. Returns (orders, safety_stock_units of the first order)."""
    lead = max(1, round(item.lead_time))
    batches = [[e, u] for e, u in item.batches]
    arrivals: dict[date, list] = {}
    for when, units in item.open_orders:
        arrivals.setdefault(max(when, today), []).append(units)

    def demand(start: date, n: int) -> float:
        return sum(fc.daily.get(start + timedelta(days=i), 0.0) for i in range(n))

    def next_order_day(after: date) -> date:
        for i in range(1, 15):
            d = after + timedelta(days=i)
            if d.weekday() in item.order_weekdays and d not in norwegian_holidays(d.year):
                return d
        return after + timedelta(days=7)

    orders, first_safety = [], None
    for i in range(days):
        day = today + timedelta(days=i)
        can_order = day.weekday() in item.order_weekdays and day not in norwegian_holidays(day.year)
        if can_order and day + timedelta(days=lead) < today + timedelta(days=days):
            arrive = day + timedelta(days=lead)
            cover_days = (next_order_day(day) - day).days                # until the next delivery
            # Simulate a copy, without this order, until the next delivery could arrive:
            # the units we'd be short in that window are what this order must bring.
            copy, short, at_arrival = [b[:] for b in batches], Result(), 0.0
            for j in range(lead + cover_days):
                d = day + timedelta(days=j)
                if j == lead:
                    at_arrival = sum(u for _, u in copy) + sum(arrivals.get(d, []))
                _step(d, copy, arrivals, fc.daily.get(d, 0.0), item.shelf_life, short if j >= lead else None)
            left_over = sum(u for _, u in copy)
            avg = demand(arrive, cover_days) / cover_days
            # safety stock = z * sqrt(demand variation over lead time + cover, + lead-time variation)
            # (cv is week-to-week, so the variation over T days is cv * avg * sqrt(7 * T))
            safety = SERVICE_Z * math.sqrt(7 * (lead + cover_days) * (fc.cv * avg) ** 2
                                           + (avg * item.lead_time_sd) ** 2)
            qty = short.lost_units + max(0.0, safety - left_over)
            # never more than can be sold before it expires (after the stock already there)
            sellable = demand(arrive, item.shelf_life) - at_arrival
            qty = min(qty, max(short.lost_units, sellable))
            if qty > 0.01:
                qty = math.ceil(qty / item.pack_size) * item.pack_size
                arrivals.setdefault(arrive, []).append(qty)
                orders.append({"order_date": day.isoformat(), "arrival_date": arrive.isoformat(), "qty": qty})
                if first_safety is None:
                    first_safety = round(float(safety), 1)
        _step(day, batches, arrivals, fc.daily.get(day, 0.0), item.shelf_life, None)
    return orders, first_safety or 0.0


# ---------- 4. one row per sku + location ----------

def _r(x, digits: int = 0):
    """Plain Python number for JSON (numpy floats sneak in from pandas)."""
    return int(round(float(x))) if digits == 0 else round(float(x), digits)


def _money(result: Result, item: Item) -> dict:
    return {"capital_tied_nok": _r(np.mean(result.stock_value_days) if result.stock_value_days else 0.0),
            "lost_sales_nok": _r(result.lost_units * item.price),
            "waste_nok": _r(result.waste_units * item.unit_cost),
            "lost_units": _r(result.lost_units, 1), "waste_units": _r(result.waste_units, 1)}


def plan_item(item: Item, fc: Forecast, today: date, days: int) -> dict:
    on_hand = sum(u for _, u in item.batches)
    on_order = sum(u for _, u in item.open_orders)
    avg_daily = sum(fc.daily.get(today + timedelta(days=i), 0.0) for i in range(days)) / days
    cover = _r(on_hand / avg_daily, 1) if avg_daily > 0 else None     # None = no expected demand

    as_is = simulate(item, fc, today, days, [])
    bau_orders = [(arrive, units) for ordered, arrive, units in item.last_year_orders if ordered >= today]
    without = simulate(item, fc, today, days, bau_orders)
    orders, safety = recommend(item, fc, today, days)
    with_ = simulate(item, fc, today, days, [(date.fromisoformat(o["arrival_date"]), o["qty"]) for o in orders])

    overstock = on_hand > 0 and (cover is None or cover > OVERSTOCK_DAYS)
    releasable = (on_hand - (avg_daily * OVERSTOCK_DAYS if cover is not None else 0)) * item.unit_cost if overstock else 0
    w, wo = _money(with_, item), _money(without, item)
    impact = (wo["lost_sales_nok"] - w["lost_sales_nok"]) + (wo["waste_nok"] - w["waste_nok"]) + releasable

    return {
        "sku": item.sku, "location_id": item.location_id,
        "action": "reduce" if overstock else ("order" if orders else "ok"),
        "days_of_cover": cover,
        "stockout_risk_date": as_is.first_stockout.isoformat() if as_is.first_stockout else None,
        "expected_waste_units": _r(as_is.waste_units, 1),
        "expected_waste_nok": _r(as_is.waste_units * item.unit_cost),
        "next_order": orders[0] if orders else None,
        "recommended_orders": orders,
        "with_recommendation": w, "without_recommendation": wo,
        "overstock": overstock,
        "capital_releasable_nok": _r(releasable),
        "nok_impact": _r(impact),
        "inputs": {
            "on_hand_units": on_hand, "on_order_units": on_order,
            "open_orders": [{"expected_date": d.isoformat(), "units": u} for d, u in item.open_orders],
            "batches": [{"expiry_date": e.isoformat(), "units": u} for e, u in sorted(item.batches)],
            "unit_cost_nok": item.unit_cost, "price_nok": item.price, "shelf_life_days": item.shelf_life,
            "pack_size": item.pack_size, "lead_time_days_avg": item.lead_time, "lead_time_days_sd": item.lead_time_sd,
            "order_weekdays": item.order_weekdays,
            "trend_factor": fc.trend, "last_8_weeks_units": _r(fc.recent_units),
            "same_8_weeks_last_year_units": _r(fc.recent_last_year_units),
            "forecast_daily_avg": _r(avg_daily, 2),
            "forecast_weekly_units": [_r(sum(fc.daily.get(today + timedelta(days=7 * w + i), 0.0)
                                                for i in range(7)), 1) for w in range(math.ceil(days / 7))],
            "stockout_days_in_history": fc.stockout_days_used,
            "promo_uplift": fc.promo_uplift, "planned_promo_days": fc.planned_promo_days,
            "safety_stock_units": safety,
            "business_as_usual_orders": len(bau_orders),
            "business_as_usual_units": sum(u for _, u in bau_orders),
        },
    }
