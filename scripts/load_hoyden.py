"""Upload the Høyden Matmarked case data (data/hoyden/*.csv) through POST /upload/table.

    .venv/bin/python -m scripts.load_hoyden http://localhost:8000
    .venv/bin/python -m scripts.load_hoyden https://ai-session-with-kpmg.onrender.com

Each CSV becomes a table named after the file. Column descriptions are written from
data/hoyden/README.md (the table list and key notes; the "fasit" section is never used).
Needs APP_API_KEY in .env.
"""
import json
import sys
import time
from pathlib import Path

import httpx

from app.config import settings

DATA = Path(__file__).resolve().parent.parent / "data" / "hoyden"

LOC = "Location id, L01-L04 (-> locations.location_id)"
SKU = "Product id, SKU001-SKU036 (-> products.sku)"
SUP = "Supplier id, S01-S08 (-> suppliers.supplier_id)"

DESCRIPTIONS = {
    "locations.csv": {
        "location_id": "Location id, L01-L04",
        "name": "Store name",
        "type": "store (physical store, closed Sundays and public holidays) or online (web shop, always open)",
        "city": "City",
        "size_index": "Relative store size (1.0 = average)",
    },
    "suppliers.csv": {
        "supplier_id": "Supplier id, S01-S08",
        "name": "Supplier name",
        "country": "Supplier country",
        "lead_time_days_avg": "Average lead time in days from order to delivery",
        "lead_time_days_sd": "Standard deviation of the lead time, in days",
        "order_weekdays": "Weekdays orders can be placed, comma separated, 0 = Monday ... 6 = Sunday (text, e.g. '0,2,4')",
        "on_time_rate": "Share of orders delivered on time (fraction, 0.97 = 97%)",
        "min_order_value_nok": "Minimum order value per order, NOK",
    },
    "products.csv": {
        "sku": "Product id, SKU001-SKU036",
        "name": "Product name (Norwegian)",
        "category": "Product category (Norwegian)",
        "supplier_id": SUP,
        "unit_cost_nok": "Purchase price per unit, NOK (stock is valued at this price)",
        "price_nok": "Normal selling price per unit, NOK",
        "shelf_life_days": "Shelf life in days from delivery",
        "storage": "Storage type: kjøl (chilled), frys (frozen), tørr (ambient)",
        "season_profile": "Demand season profile, e.g. flat, christmas_only, christmas_strong, summer, easter_only",
        "pack_size": "Units per pack; orders are placed in whole packs",
    },
    "calendar.csv": {
        "date": "Date (one row per day, 2024-11-01 to 2026-11-05)",
        "weekday": "Weekday name in English",
        "week": "ISO week number",
        "month": "Month number",
        "public_holiday": "Name of the Norwegian public holiday, empty if none (stores closed)",
        "season": "Season (Norwegian): vinter, vår, sommer, høst",
        "event": "Trading event, empty if none: Julehandel (start/topp) = Christmas trade, Påske = Easter, "
                 "17. mai, Fellesferie/sommer = summer holiday",
    },
    "promotions.csv": {
        "promo_id": "Promotion id",
        "sku": SKU,
        "start_date": "First day of the promotion",
        "end_date": "Last day of the promotion",
        "discount_pct": "Discount as a fraction (0.2 = 20% off)",
        "locations": "Locations the promotion applies to ('all' = every location)",
    },
    "sales_daily.csv": {
        "date": "Sales date. Days a store is closed (Sundays, public holidays) have no row",
        "location_id": LOC,
        "sku": SKU,
        "units_sold": "Units sold that day",
        "revenue_nok": "Revenue that day, NOK",
        "unit_price_nok": "Selling price per unit that day, NOK (lower on promotion)",
        "on_promotion": "True if the product was on promotion that day",
        "stockout": "True if the product ran out that day: demand was not met, so units_sold is lower than demand",
    },
    "sales_weekly.csv": {
        "week_start": "Monday of the week",
        "location_id": LOC,
        "sku": SKU,
        "units_sold": "Units sold that week",
        "revenue_nok": "Revenue that week, NOK",
        "stockout_days": "Number of days that week the product was sold out",
        "promo_days": "Number of days that week the product was on promotion",
    },
    "inventory_weekly.csv": {
        "week_ending": "Sunday the stock was counted (Sunday evening)",
        "location_id": LOC,
        "sku": SKU,
        "units_on_hand": "Units in stock",
        "stock_value_nok": "Stock value at purchase price (unit_cost_nok), NOK",
        "units_on_order": "Units ordered but not yet received",
    },
    "inventory_current.csv": {
        "as_of": "Date of the stock count (yesterday evening)",
        "location_id": LOC,
        "sku": SKU,
        "units_on_hand": "Units in stock now",
        "units_on_order": "Units ordered but not yet received (open purchase orders)",
        "stock_value_nok": "Stock value at purchase price, NOK",
    },
    "inventory_batches_current.csv": {
        "location_id": LOC,
        "sku": SKU,
        "units": "Units in this batch",
        "expiry_date": "Last day the batch can be sold",
        "days_to_expiry": "Days from today until expiry_date (waste risk when low)",
    },
    "purchase_orders.csv": {
        "po_id": "Purchase order id",
        "supplier_id": SUP,
        "sku": SKU,
        "location_id": LOC,
        "order_date": "Date the order was placed",
        "expected_date": "Expected delivery date",
        "qty_ordered": "Units ordered",
        "received_date": "Actual delivery date, empty if not received yet",
        "qty_received": "Units received, empty if not received yet",
        "unit_cost_nok": "Purchase price per unit, NOK",
        "status": "mottatt = received, åpen = open (not received yet)",
    },
    "waste.csv": {
        "date": "Date the units were written off",
        "location_id": LOC,
        "sku": SKU,
        "units": "Units written off",
        "reason": "utløpt = expired, skadet = damaged",
        "cost_nok": "Cost of the written-off units at purchase price, NOK",
    },
}


def main() -> int:
    base = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8000"
    headers = {"X-API-Key": settings.app_api_key}
    files = sorted(DATA.glob("*.csv"))
    missing = set(DESCRIPTIONS) - {f.name for f in files}
    if missing:
        print("Missing files:", ", ".join(sorted(missing)))
        return 1
    with httpx.Client(timeout=600) as client:
        for path in files:
            started = time.monotonic()
            r = client.post(f"{base}/upload/table", headers=headers,
                            files={"file": (path.name, path.read_bytes(), "text/csv")},
                            data={"column_descriptions": json.dumps(DESCRIPTIONS.get(path.name, {}))})
            if r.status_code != 200:
                print(f"{path.name}: HTTP {r.status_code} {r.text[:300]}")
                return 1
            body = r.json()
            t = body["tables"][0]
            print(f"{path.name:32} -> {t['table']:28} {t['rows']:>6} rows  {time.monotonic() - started:5.1f}s"
                  + (f"  unused descriptions: {body['unused_column_descriptions']}" if body["unused_column_descriptions"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
