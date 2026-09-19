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
This was validated end-to-end against the live pool (pH 8.0, ORP set point 800,
chlorine set point 3, ORP 706 off the chart, status OK). Just confirm it works
in your environment:

```bash
./venv/bin/python cmp_pool.py --debug
```

- Expect sane `ph` / `orp` / `orp_status` and `has_data: true` (as long as the
  pump has run within the last 24h).
- If something is off, inspect the saved artifacts in `/tmp/cmp_debug/`:
  - `chemistry.html` — the Chemistry page. pH is the `lblPHMeasure` span, the
    set points are `lblORPSetPoint` / `lblChlorineSetPoint`. If these come back
    empty, `lblMessage` says why (usually "Please select a pool first").
  - `dashboard.html` — the populated dashboard: pump state and the warning grid.
  - `orp_chart.png` — the ORP chart the pixel-scraper read.

pH, the ORP status and both set points are **exact** (read straight from the
page). Only the ORP *number* is a pixel-scrape of the chart (~±3): the site
shows ORP as a status word, never as an mV figure in text.

### ORP chart calibration (scale-independent)
The ORP chart's y-axis auto-rescales, so nothing is hard-wired. Calibration
order:

1. **Primary — OCR the y-axis labels** (needs the `tesseract` binary +
   `pytesseract`). The scraper reads the printed axis numbers directly, so it
   works at any scale. `orp_meta.source == "ocr_axis"`.
   - Install: `apt install tesseract-ocr` (Debian/Ubuntu) or
     `brew install tesseract` (macOS), plus `pip install pytesseract`.
2. **Fallback — fixed anchor** (`orp_meta.source == "gridline_fallback"`), used
   only if tesseract is missing. It maps pixels via `orp_units_per_gridline`
   and anchors the orange set-point line to the exact value read from
   Chemistry.aspx. `orp_units_per_gridline` is the one number it cannot derive:
   the axis switches between 10 and 20 units per gridline as it rescales, and
   getting it wrong skews ORP by tens of mV. **Install tesseract** rather than
   rely on this path.

`orp_meta.setpoint` is the set point as read off the chart. It is a cross-check
on the calibration, not the reported value — `orp_setpoint` comes from
Chemistry.aspx and only falls back to the chart if that page fails
(`chem_error` is then non-null).

Check `orp_meta.source` in the output to see which path ran. If drift appears,
inspect `/tmp/cmp_debug/orp_chart.png`.

### How the scrape works (for future maintenance)
Four non-obvious things make or break it:
1. **Login** needs HTTPS + a `User-Agent` header, else no `.ASPXAUTH` cookie.
2. **The dashboard** needs the pool's `btnDash` button on PoolShop.aspx. It
   302-redirects to `Dashboard.aspx?Data=<signed-token>` — that signed token
   (not `?PoolSystemID=`) is what renders a populated dashboard. Following the
   redirect gives the values directly; no further AJAX/postback is needed.
3. **Chemistry.aspx needs a different selection**: the grid's "View / Monitor
   Pool" button (`btnControl`). The dashboard's signed token does *not* select
   the pool for it — without the `btnControl` post the page renders every value
   span empty and `lblMessage` reads "Please select a pool first". Driving its
   `updpnlChemistry` UpdatePanel timer postback does not help either.
4. The ORP chart image URL's filename index changes per render, so the scraper
   keys off the `ORPChart` element id, not the filename.

## Schedule it (cron, hourly)
```cron
0 * * * * /opt/connectmypool-ha/venv/bin/python /opt/connectmypool-ha/cmp_pool.py >/dev/null 2>&1
```
The password can live in `config.json` (`chmod 600` it), in which case the cron
line needs nothing extra — as above. If you'd rather keep it out of the file,
omit `password` from `config.json` and prepend the env var instead:
```cron
0 * * * * CMP_PASS='yourpassword' /opt/connectmypool-ha/venv/bin/python /opt/connectmypool-ha/cmp_pool.py >/dev/null 2>&1
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

### Alternative: `platform: rest` + template sensors
If you use the `sensor:` platform style, note it makes one HTTP request per
sensor entry. To get separate entities from a single fetch, use one REST sensor
plus a `template:` block that splits it. `value_json` is the parsed JSON body,
so `value_json.ph` is the file's `ph` field, etc.

```yaml
sensor:
  - platform: rest
    name: Pool Chemistry
    resource: https://www.avenard.org/pool/pool.json
    method: GET
    value_template: "{{ value_json.ph }}"   # sensor state; arbitrary pick
    json_attributes:
      - ph
      - orp
      - orp_status
      - orp_setpoint
      - pump_speed
      - pump_state
      - last_orp
      - last_ph
      - last_warning
      - warnings
      - has_data
      - ok
      - ts
    scan_interval: 300
    verify_ssl: true
    headers:
      User-Agent: Home Assistant

template:
  - sensor:
      - name: Pool pH
        state: "{{ state_attr('sensor.pool_chemistry','ph') }}"
        availability: "{{ state_attr('sensor.pool_chemistry','has_data') }}"
        state_class: measurement
      - name: Pool ORP
        state: "{{ state_attr('sensor.pool_chemistry','orp') }}"
        unit_of_measurement: mV
        availability: "{{ state_attr('sensor.pool_chemistry','has_data') }}"
        state_class: measurement
      - name: Pool ORP Status
        state: "{{ state_attr('sensor.pool_chemistry','orp_status') }}"
      - name: Pool ORP Set Point
        state: "{{ state_attr('sensor.pool_chemistry','orp_setpoint') }}"
        unit_of_measurement: mV
      - name: Pool Last Warning
        state: "{{ state_attr('sensor.pool_chemistry','last_warning') }}"
        attributes:
          warnings: "{{ state_attr('sensor.pool_chemistry','warnings') }}"
```

`pool.json` is a static file refreshed hourly by cron, so poll it gently
(`scan_interval: 300`); don't reuse the aggressive interval you'd use against
the live API.

## Output shape
```json
{
  "ph": 8.0,
  "orp": 706,
  "orp_status": "OK",
  "orp_setpoint": 800,
  "chlorine_setpoint": 3,
  "system_status": "Pool system online",
  "pump_speed": "AUTO",
  "pump_state": "Last ran: 2 minutes ago",
  "last_orp": "Last measured: 11 minutes ago",
  "last_ph": "Last measured: 8 minutes ago",
  "warnings": [
    {"date": "Sep 19 2026 14:34", "description": "pH has been outside set point by over 0.4 for more than 2 hours"}
  ],
  "last_warning": "pH has been outside set point by over 0.4 for more than 2 hours",
  "orp_meta": {"source": "ocr_axis", "labels": 9, "px_per_unit": 0.46, "setpoint": 800},
  "chem_error": null,
  "has_data": true,
  "ts": 1789825713,
  "ok": true
}
```
- `ph`, `orp_status`, `orp_setpoint`, `chlorine_setpoint` and `system_status`
  are read verbatim from Chemistry.aspx (exact). `orp` is the chart
  pixel-scrape (approx ±3).
- `chem_error` is non-null when Chemistry.aspx could not be read; `orp_setpoint`
  then falls back to `orp_meta.setpoint` (the chart) and `ph` to the dashboard.
- `orp_meta.source`: `ocr_axis` = calibrated from the OCR'd y-axis labels
  (scale-independent); `gridline_fallback` = OCR unavailable, mapped pixels with
  the configured `orp_units_per_gridline`.
- `orp_meta.setpoint` is the set point read off the orange line — a sanity check
  on the calibration; it should match `orp_setpoint`.
- `has_data` is false only when the chart is empty (no pump run in the last
  24h). The `availability` templates above make HA hold the last reading instead
  of showing "unknown".
- `orp_status` is the raw text of the ORP status field ("OK" or, in an alarm
  state, whatever the site shows there); `null` when there's no reading. The
  site never publishes ORP in mV as text, which is why `orp` comes from pixels.
- `warnings` is the dashboard's Warning/Fault grid (most recent first, up to
  10); `last_warning` is the newest description, `null` if none.
- `ok`/`ts` — `ok:false` means the scrape itself failed (the error goes to
  `pool.json.err`); use `ts` to detect staleness.
