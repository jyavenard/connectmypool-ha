# ConnectMyPool → Home Assistant (via Apache + REST sensor)

Scrapes **ORP** and **pH** from the ConnectMyPool installer dashboard and writes
`pool.json` into your Apache doc root. Home Assistant reads that file with a
`rest` sensor. HA never talks to the pool site — the scrape runs on a timer you
control, so HA polls stay instant and you don't hammer ConnectMyPool.

```
ConnectMyPool site ──(cron: cmp_pool.py)──▶ /var/www/html/pool.json ──(Apache)──▶ HA rest sensor
```

## Why scraping and not the API
The control API (`/api/poolaction`, `poolstatus`, `poolconfig`) exposes only
equipment state — temperature, heaters, channels, lights. There is **no
chemistry endpoint** (`orp`, `ph`, `chemistry`, `alarms` all return 404).
pH is shown on the dashboard as a number; ORP only as an "OK"/alarm status, with
the numeric ORP living **only inside the chart image**. The dashboard is ASP.NET
WebForms and loads values via a timer-driven async postback, so a plain GET has
nothing — which is why HA's own `rest` sensor can't scrape it directly.

## Install (on the Apache machine)
```bash
cd /opt && sudo git clone <this> connectmypool-ha   # or just copy the folder
cd connectmypool-ha
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp config.example.json config.json
# edit config.json: set the password (or leave it and use the CMP_PASS env var)
```

## Readings can be stale (ORP only samples while the pump runs)
The scraper reads the **rightmost (most recent) point** of the 24-hour chart.
ORP is only *sampled* while the filter pump runs (chart legend: "ORP (Pump
Running)"), so the last point may be minutes or hours old — check `last_orp`
("Last measured: N minutes ago"). Values are still present when the pump is
currently off, as long as it ran at some point within the 24-hour window.
`has_data` is only false if the chart is genuinely empty (no pump run in that
window); the previous `pool.json` is then left in place.

## First run — verify
This was validated end-to-end against the live pool (pH 7.6, ORP 637 vs. the
dashboard's ~635, status OK, setpoint 690, pump "MEDIUM SPEED / Running"). Just
confirm it works in your environment:

```bash
./venv/bin/python cmp_pool.py --debug
```

- Expect sane `ph` / `orp` / `orp_status` and `has_data: true` (as long as the
  pump has run within the last 24h).
- If something is off, inspect the saved artifacts in `/tmp/cmp_debug/`:
  - `dashboard.html` — the populated page. pH is the `phMeasureLBL` span, ORP
    status the `orpMeasureLBL` span.
  - `orp_chart.png` — the ORP chart the pixel-scraper read. If ORP drifts, check
    `orp_units_per_gridline` and `orp_setpoint` (the orange line's value).

pH and ORP status are **exact** (read straight from the page). The ORP *number*
is a pixel-scrape of the chart (~±3).

### ORP chart calibration (scale-independent)
The ORP chart's y-axis auto-rescales and the setpoint can change, so nothing is
hard-wired. Calibration order:

1. **Primary — OCR the y-axis labels** (needs the `tesseract` binary +
   `pytesseract`). The scraper reads the printed axis numbers directly, so it
   works at any scale, and it also reads the **setpoint dynamically** from the
   orange line's position. `orp_meta.source == "ocr_axis"`.
   - Install: `apt install tesseract-ocr` (Debian/Ubuntu) or
     `brew install tesseract` (macOS), plus `pip install pytesseract`.
2. **Fallback — fixed anchor** (`orp_meta.source == "gridline_fallback"`), used
   only if tesseract is missing. Maps pixels via `orp_units_per_gridline` and a
   known anchor (`orp_setpoint`, else `orp_axis_top`). These are the *only*
   hard-wired numbers, and only this path uses them — update them if your axis
   rescales while running without tesseract.

Check `orp_meta.source` in the output to see which path ran. If drift appears,
inspect `/tmp/cmp_debug/orp_chart.png`.

### How the scrape works (for future maintenance)
Three non-obvious things make or break it:
1. **Login** needs HTTPS + a `User-Agent` header, else no `.ASPXAUTH` cookie.
2. **Pool selection**: click the pool's Dashboard button (`btnDash`) on
   PoolShop.aspx. It 302-redirects to `Dashboard.aspx?Data=<signed-token>` — that
   signed token (not `?PoolSystemID=`) is what selects the pool and renders a
   fully populated dashboard. Following that redirect gives the values directly;
   no further AJAX/postback is needed.
3. The ORP chart image URL's filename index changes per render, so the scraper
   keys off the `ORPChart` element id, not the filename.

## Schedule it (cron, every 10 min)
```cron
*/10 * * * * /opt/connectmypool-ha/venv/bin/python /opt/connectmypool-ha/cmp_pool.py >/dev/null 2>&1
```
The password can live in `config.json` (`chmod 600` it), in which case the cron
line needs nothing extra — as above. If you'd rather keep it out of the file,
omit `password` from `config.json` and prepend the env var instead:
```cron
*/10 * * * * CMP_PASS='yourpassword' /opt/connectmypool-ha/venv/bin/python /opt/connectmypool-ha/cmp_pool.py >/dev/null 2>&1
```
`CMP_USER`/`CMP_PASS` override whatever is in `config.json` when set.

The JSON is written atomically, so Apache never serves a half-written file. On a
failed scrape the last good `pool.json` is left untouched and the error is
written to `pool.json.err`.

## Apache
`json_out` points at `/var/www/html/pool.json`, so Apache serves it at
`http://<host>/pool.json` with no extra config. Make sure the cron user can
write there (or point `json_out` somewhere writable and `Alias` it). To restrict
access, drop it behind a `Location` with your LAN allowlist.

## Home Assistant — REST sensor
`configuration.yaml`:
```yaml
rest:
  - resource: "http://<apache-host>/pool.json"
    scan_interval: 300
    sensor:
      - name: "Pool pH"
        value_template: "{{ value_json.ph }}"
        # hold the last value when data is missing instead of going "unknown"
        availability: "{{ value_json.has_data }}"
        unique_id: pool_ph
        state_class: measurement
      - name: "Pool ORP"
        value_template: "{{ value_json.orp }}"
        availability: "{{ value_json.has_data }}"
        unit_of_measurement: "mV"
        unique_id: pool_orp
        state_class: measurement
      - name: "Pool ORP Status"
        value_template: "{{ value_json.orp_status }}"
        unique_id: pool_orp_status
      - name: "Pool ORP Set Point"
        value_template: "{{ value_json.orp_setpoint }}"
        unit_of_measurement: "mV"
        unique_id: pool_orp_setpoint
      - name: "Pool Pump"
        value_template: "{{ value_json.pump_state }}"
        json_attributes_path: "$"
        json_attributes:
          - pump_speed
          - last_orp
          - last_ph
        unique_id: pool_pump
      - name: "Pool Last Warning"
        value_template: "{{ value_json.last_warning }}"
        json_attributes_path: "$"
        json_attributes:
          - warnings
        unique_id: pool_last_warning
```

## Output shape
```json
{
  "ph": 7.6,
  "orp": 638,
  "orp_status": "OK",
  "orp_setpoint": 690,
  "pump_speed": "AUTO",
  "pump_state": "Last ran: 2 minutes ago",
  "last_orp": "Last measured: 11 minutes ago",
  "last_ph": "Last measured: 8 minutes ago",
  "warnings": [
    {"date": "Aug 7 2026 03:05", "description": "Heating has been left on for more than 12 hours"}
  ],
  "last_warning": "Heating has been left on for more than 12 hours",
  "orp_meta": {"source": "ocr_axis", "labels": 11, "px_per_unit": 2.0, "setpoint": 690},
  "has_data": true,
  "ts": 1754730000,
  "ok": true
}
```
- `ph` and `orp_status` are read verbatim from the page (exact). `orp` is the
  chart pixel-scrape (approx ±3).
- `orp_meta.source`: `ocr_axis` = calibrated from the OCR'd y-axis labels
  (scale-independent; `setpoint` is then read off the orange line too);
  `gridline_fallback` = OCR unavailable, used the configured anchor.
- `has_data` is false only when the chart is empty (no pump run in the last
  24h). The `availability` templates above make HA hold the last reading instead
  of showing "unknown".
- `orp_status` is the raw text of the dashboard's ORP status field ("OK" or,
  in an alarm state, whatever the site shows there); `null` when there's no
  reading.
- `warnings` is the dashboard's Warning/Fault grid (most recent first, up to
  10); `last_warning` is the newest description, `null` if none.
- `ok`/`ts` — `ok:false` means the scrape itself failed (the error goes to
  `pool.json.err`); use `ts` to detect staleness.
