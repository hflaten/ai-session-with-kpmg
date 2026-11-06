<!-- Business context for the current case, added to every agent's system prompt
     (app/agent/service.py). Copied from data/hoyden/README.md WITHOUT the 'fasit' section.
     Replace or empty this file for a new case. -->

# Høyden Matmarked: syntetiske data (KPMG Lovable-case 4)

Oppdiktede, men realistiske data for en liten premium dagligvarekjede med tre butikker og nettbutikk.
Periode: 1. nov 2024 til 5. nov 2026 (i går). "I dag" er 6. nov 2026, sju uker før jul.
Generert med en simulering: daglig etterspørsel (ukedag, sesong, høytider, kampanjer, trend, støy),
FIFO-lager med holdbarhet, innkjøp med ledetid og leverandørforsinkelser, og svinn.
Alle beløp i NOK. Butikkene er stengt søndager og helligdager; nettbutikken er alltid åpen.

## Tabeller

| Fil | Rader | Innhold |
|---|---|---|
| locations.csv | 4 | Butikker og nettbutikk (L01-L04), size_index = relativ størrelse |
| suppliers.csv | 8 | Ledetid (snitt/sd, dager), bestillingsdager (0=man), leveringspresisjon, minste ordreverdi |
| products.csv | 36 | Varer: kategori, leverandør, innpris, utpris, holdbarhet (dager), lagring, sesongprofil, pakningsstørrelse |
| calendar.csv | 735 | Dato, ukedag, uke, måned, helligdag, sesong, hendelse (jul, påske, 17. mai, sommer, Black Friday) |
| promotions.csv | 36 | Kampanjer med rabatt, inkl. to planlagte fremover (laks 1.-10. des, olivenolje 16.-29. nov) |
| sales_daily.csv | ~92 000 | Salg per dag, lokasjon og vare: enheter, omsetning, pris, kampanje, stockout (udekket etterspørsel) |
| sales_weekly.csv | ~15 000 | Samme aggregert per uke, inkl. antall utsolgt-dager. Bruk denne i Lovable hvis daglig blir tungt |
| inventory_weekly.csv | ~15 000 | Lagerbeholdning hver søndag kveld: enheter, lagerverdi (innpris), enheter i bestilling |
| inventory_current.csv | 144 | Lager per vare og lokasjon nå: på lager, i bestilling, lagerverdi |
| inventory_batches_current.csv | ~190 | Partier på lager nå med utløpsdato og dager til utløp (svinnrisiko) |
| purchase_orders.csv | ~20 000 | Innkjøpsordrer: bestilt, forventet, mottatt dato og mengde, status (mottatt/åpen) |
| waste.csv | ~5 800 | Svinn per dag: enheter, årsak (utløpt/skadet), kostnad |

Nøkler: sku -> products, location_id -> locations, supplier_id -> suppliers, date -> calendar.
