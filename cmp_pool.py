#!/usr/bin/env python3
"""
ConnectMyPool -> JSON bridge (for Home Assistant via a REST sensor).

Runs on a machine with Apache (or any web server), on a cron/systemd timer.
Scrapes ORP and pH from the ConnectMyPool *installer* dashboard and writes a
JSON file into the web root. Home Assistant then polls that file with a `rest`
sensor -- HA never touches the pool site directly, so polls are instant and the
fragile scrape happens on a schedule you control.

Why scraping (not the API):
  * The control API (/api/poolaction etc.) returns equipment state only
    (temperature, heaters, channels, lights). No chemistry endpoint exists
    (orp/ph/chemistry/alarms all 404). Confirmed by probing.
  * pH and an ORP status ("OK") are shown on the dashboard; the numeric ORP
    lives ONLY inside the rendered chart image.
  * The dashboard is ASP.NET WebForms and the pool must be selected via a signed
    redirect token, so a plain GET has nothing -- which is why HA's native rest
    sensor cannot scrape the site itself.

Flow (validated live):
  login (HTTPS + User-Agent) -> GET PoolShop.aspx -> POST the pool's Dashboard
  button (btnDash), which 302-redirects to Dashboard.aspx?Data=<signed-token>;
  following that redirect yields the fully populated dashboard. Then:
    * pH        -> exact, from the phMeasureLBL span
    * ORP status-> exact, from the orpMeasureLBL span ("OK")
    * ORP value -> pixel analysis of the ORP chart image (~+/-3), self-calibrated
                   from gridline spacing + the orange Set Point line.

Run it on your OWN network. NB: every postback must target the exact form-action
URL (query string included) or ASP.NET rejects it with "viewstate MAC failed".
"""

import io
import json
import os
import re
import sys
import time
import argparse
import statistics
import tempfile

import requests

try:
    from PIL import Image
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

try:
    import pytesseract          # optional: enables reading the chart's y-axis
    HAVE_TESS = True            # labels for scale-independent ORP calibration
except ImportError:
    HAVE_TESS = False


DEFAULT_CONFIG = {
    "base_url": "https://www.connectmypool.com.au",
    "username": "",
    "password": "",
    "pool_name": "JYA Pool",          # picks the right row on the PoolShop list
    "user_agent": "Mozilla/5.0",

    # pH and ORP status are read verbatim from fixed dashboard spans
    # (phMeasureLBL / orpMeasureLBL), so no extraction regexes are configurable.
    # --- ORP chart calibration ----------------------------------------------
    # PRIMARY: OCR the chart's printed y-axis labels (needs tesseract). This is
    # scale-independent -- it survives the axis auto-rescaling and setpoint
    # changes, because it reads the actual numbers off the image.
    "tesseract_cmd": None,            # path to tesseract; None => auto-detect
    # FALLBACK (used only if OCR is unavailable/fails): map pixels using a fixed
    # gridline step and a known anchor. Update these if your axis rescales.
    "orp_units_per_gridline": 10,     # ORP units between horizontal gridlines
    "orp_setpoint": 690,              # value of the orange Set Point line
    "orp_axis_top": 710,              # value of the top gridline

    # --- output --------------------------------------------------------------
    # Write the JSON into your Apache doc root so HA can fetch it over HTTP.
    "json_out": "/var/www/html/pool.json",
    "debug_dir": "/tmp/cmp_debug",
    "retries": 3,                      # retry whole login+scrape on MAC failure
}


def load_config(path):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path and os.path.exists(path):
        for k, v in json.load(open(path)).items():
            cfg[k] = v
    cfg["username"] = os.environ.get("CMP_USER", cfg["username"])
    cfg["password"] = os.environ.get("CMP_PASS", cfg["password"])
    return cfg


# --------------------------------------------------------------------------- #
# Site interaction
# --------------------------------------------------------------------------- #
def hidden_fields(html):
    """Grab ALL hidden <input> fields (__VIEWSTATE, __EVENTVALIDATION,
    __VIEWSTATEGENERATOR, __VIEWSTATEENCRYPTED, __EVENTTARGET, ...). Sending the
    full set is what a real browser posts back."""
    f = {}
    for m in re.finditer(r'<input[^>]*type="hidden"[^>]*>', html):
        tag = m.group(0)
        n = re.search(r'name="([^"]*)"', tag)
        v = re.search(r'value="([^"]*)"', tag)
        if n:
            f[n.group(1)] = v.group(1) if v else ""
    return f


def login(session, cfg):
    B = cfg["base_url"]
    r = session.get(B + "/Front/Login.aspx", timeout=30)
    r.raise_for_status()
    f = hidden_fields(r.text)
    f.update({
        "ucLogin1$txtUserName": cfg["username"],
        "ucLogin1$txtPassword": cfg["password"],
        "ucLogin1$chkRememberMe": "on",
        "ucLogin1$btnLogin": "Login",
    })
    session.post(B + "/Front/Login.aspx", data=f, timeout=30).raise_for_status()
    if ".ASPXAUTH" not in session.cookies:
        raise RuntimeError("Login failed: no .ASPXAUTH cookie. Check credentials.")


def fetch_dashboard(session, cfg):
    """Click the Dashboard (btnDash) button for our pool. That posts to
    PoolShop.aspx and 302-redirects to Dashboard.aspx?Data=<signed-token>, which
    is what actually selects the pool AND renders the populated dashboard
    (pH/ORP values + charts). requests follows the redirect, so the returned HTML
    is already the populated page -- no further postback needed."""
    B = cfg["base_url"]
    ps = session.get(B + "/Account/PoolShop.aspx", timeout=30).text
    idx = ps.find(cfg["pool_name"])
    if idx < 0:
        raise RuntimeError("pool %r not found in the PoolShop list" % cfg["pool_name"])
    best = None
    for mm in re.finditer(r'name="(ctl00\$cpPageContent\$gvPoolSystem\$ctl\d+\$btnDash)"', ps):
        if best is None or abs(mm.start() - idx) < abs(best.start() - idx):
            best = mm
    if best is None:
        raise RuntimeError("no Dashboard button found for %r" % cfg["pool_name"])
    f = hidden_fields(ps)
    f[best.group(1) + ".x"] = "5"
    f[best.group(1) + ".y"] = "5"
    r = session.post(B + "/Account/PoolShop.aspx", data=f, timeout=30, allow_redirects=True)
    return r.text


# --------------------------------------------------------------------------- #
# Value parsing
# --------------------------------------------------------------------------- #
def parse_ph(html, cfg):
    # pH is a fixed span in the populated dashboard (read verbatim, like ORP).
    v = parse_label(html, "phMeasureLBL")
    try:
        return float(v) if v else None
    except ValueError:
        return None


def parse_orp_status(html, cfg):
    # Report whatever the ORP status span actually shows ("OK" or an alarm
    # word/value) -- don't collapse everything non-OK to a single label. Empty
    # (pump off / no reading) -> None.
    return parse_label(html, "orpMeasureLBL") or None


def parse_label(html, label_id):
    m = re.search(r'id="cpPageContent_ucDashboard_%s">([^<]*)<' % label_id, html)
    return m.group(1).strip() if m else None


def parse_warnings(html, limit=10):
    """Extract the dashboard's Warning/Fault grid (gvWarning) as a list of
    {date, description}, most recent first. Empty when 'No warnings found'."""
    m = re.search(r'<table[^>]*id="cpPageContent_ucDashboard_gvWarning".*?</table>',
                  html, re.S)
    if not m:
        return []
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(0), re.S):
        cells = [re.sub(r"<[^>]+>", "", c).replace("&nbsp;", "").strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        cells = [c for c in cells if c]
        # data rows start with a date like "Aug 7 2026 03:05"; header uses <th>
        if len(cells) >= 2 and re.match(r"[A-Z][a-z]{2}\s+\d", cells[0]):
            out.append({"date": cells[0], "description": cells[1]})
            if len(out) >= limit:
                break
    return out


def find_orp_chart_url(html, base_url):
    # The numeric ORP lives only in the ORP chart image (id ...ORPChart). Its
    # index in the filename varies per render, so key off the element id.
    m = re.search(r'id="cpPageContent_ucDashboard_ORPChart"[^>]*src="([^"]+)"', html)
    if not m:
        return None
    u = m.group(1).replace("&amp;", "&")
    return u if u.startswith("http") else base_url.rstrip("/") + "/" + u.lstrip("/")


# --------------------------------------------------------------------------- #
# Chart pixel analysis (fallback for numeric ORP)
# --------------------------------------------------------------------------- #
def _is_blue(r, g, b):
    return b > 150 and b > r + 25 and g >= r and (b - g) > 10 and r < 170


def _is_orange(r, g, b):
    return r > 200 and 120 < g < 200 and b < 110


def _is_grid(r, g, b):
    return abs(r - g) < 12 and abs(g - b) < 12 and 200 < r < 240


def _ocr_axis_value_fn(im, cfg):
    """OCR the chart's printed y-axis labels and return (value_fn, meta) mapping
    a pixel row -> ORP value. Scale-independent: reads the real numbers, so it
    survives axis auto-rescaling and setpoint changes. Returns (None, meta) if
    tesseract is unavailable or the labels can't be read reliably.

    The labels are a regular arithmetic sequence of decades (e.g. 600..700).
    OCR of the small font is noisy -- it drops trailing zeros ("700"->"70") or
    misreads them ("700"->"701") -- so we recover 2-digit reads (x10), snap
    everything to the nearest 10, then fit a robust Theil-Sen line and reject
    outliers. That tolerates several bad reads."""
    if not HAVE_TESS:
        return None, {"ocr": "pytesseract not installed"}
    cmd = cfg.get("tesseract_cmd") or _which_tesseract()
    if not cmd:
        return None, {"ocr": "tesseract binary not found"}
    pytesseract.pytesseract.tesseract_cmd = cmd
    W, H = im.size
    y0 = int(H * 0.085)
    strip = im.crop((0, y0, int(W * 0.10), int(H * 0.88)))
    strip = strip.resize((strip.width * 6, strip.height * 6), Image.LANCZOS)
    try:
        dd = pytesseract.image_to_data(
            strip, config="--psm 6 -c tessedit_char_whitelist=0123456789",
            output_type=pytesseract.Output.DICT)
    except Exception as e:                                    # noqa: BLE001
        return None, {"ocr": "tesseract error: %s" % e}

    pts = []
    for i, t in enumerate(dd["text"]):
        t = t.strip()
        if not t.isdigit():
            continue
        v = int(t)
        if len(t) == 2:            # trailing zero dropped by OCR ("70" -> 700)
            v *= 10
        if len(t) > 3 or not (300 <= v <= 1000):
            continue
        v = round(v / 10.0) * 10   # labels are decades
        yc = y0 + (dd["top"][i] + dd["height"][i] / 2.0) / 6.0
        pts.append((v, yc))
    if len(pts) < 4:
        return None, {"ocr": "read %d axis labels" % len(pts)}

    def theil_sen(points):
        sl = [(points[a][0] - points[b][0]) / (points[a][1] - points[b][1])
              for a in range(len(points)) for b in range(a + 1, len(points))
              if points[a][1] != points[b][1]]
        B = statistics.median(sl)                    # ORP units per pixel (<0)
        A = statistics.median(v - B * y for v, y in points)
        return A, B

    A, B = theil_sen(pts)
    inl = [(v, y) for v, y in pts if abs(v - (A + B * y)) <= 7]  # reject misreads
    if len(inl) < 4:
        return None, {"ocr": "only %d consistent labels" % len(inl)}
    A, B = theil_sen(inl)
    if not (0.1 <= -B <= 5.0):     # sanity: plausible px<->value slope
        return None, {"ocr": "implausible axis slope %.3f" % B}
    ys = [y for _, y in inl]
    meta = {"source": "ocr_axis", "labels": len(inl), "px_per_unit": round(-1.0 / B, 3),
            "y_lo": min(ys), "y_hi": max(ys)}   # plot bounds = label extent
    return (lambda y: A + B * y), meta


def _which_tesseract():
    import shutil
    return (shutil.which("tesseract")
            or next((p for p in ("/opt/homebrew/bin/tesseract",
                                 "/usr/local/bin/tesseract",
                                 "/usr/bin/tesseract") if os.path.exists(p)), None))


def _blue_line_columns(px, W, y_lo, y_hi):
    """Median y of the blue line per column, inside [y_lo, y_hi]."""
    cols = {}
    for x in range(int(W * 0.13), W):
        ys = [y for y in range(y_lo, y_hi + 1) if _is_blue(*px[x, y])]
        if ys:
            cols[x] = statistics.median(ys)
    return cols


def _orange_setpoint(px, W, y_lo, y_hi, value_fn):
    """The orange horizontal line is the ORP Set Point. Find its row and map it
    through the calibration to read the *current* setpoint value dynamically."""
    oy = [y for y in range(y_lo, y_hi + 1)
          if sum(1 for x in range(int(W * 0.2), int(W * 0.85)) if _is_orange(*px[x, y])) > 20]
    return round(value_fn(statistics.median(oy))) if oy else None


def orp_from_pixels(png_bytes, cfg):
    if not HAVE_PIL:
        return None, {"error": "Pillow not installed"}
    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    W, H = im.size
    px = im.load()

    # PRIMARY calibration: OCR the y-axis labels (scale-independent).
    value, meta = _ocr_axis_value_fn(im, cfg)
    if value is not None:
        # bound the search to the plot area (label y-extent) so the bottom
        # legend swatch isn't mistaken for the latest data point
        y_lo, y_hi = int(meta["y_lo"]) - 4, int(meta["y_hi"]) + 4
        cols = _blue_line_columns(px, W, y_lo, y_hi)
        if not cols:
            return None, dict(meta, error="no blue line pixels found")
        sp = _orange_setpoint(px, W, y_lo, y_hi, value)
        if sp is not None:
            meta["setpoint"] = sp     # dynamic: read off the chart, not config
        meta.pop("y_lo", None); meta.pop("y_hi", None)
        return round(value(cols[max(cols)])), meta

    # FALLBACK calibration: gridline spacing + a known anchor (setpoint/axis_top).
    ocr_note = meta
    # horizontal gridlines (rows that are mostly gray across the plot)
    rows = []
    for y in range(H):
        c = sum(1 for x in range(int(W * 0.15), int(W * 0.9)) if _is_grid(*px[x, y]))
        if c > (W * 0.5):
            rows.append(y)
    collapsed = []
    for y in rows:
        if not collapsed or y - collapsed[-1] > 3:
            collapsed.append(y)
    # drop the chart's outer frame lines (top/bottom border), keep interior grid
    collapsed = [y for y in collapsed if 8 <= y <= H - 8]
    if len(collapsed) < 2:
        return None, {"error": "could not detect gridlines", "rows": collapsed}
    # unit spacing = smallest gap between adjacent gridlines (a doubled gap means
    # one line was obscured, e.g. by the setpoint line, so use the minimum)
    spacing = min(d for d in (b - a for a, b in zip(collapsed, collapsed[1:])) if d >= 8)
    px_per_unit = spacing / float(cfg["orp_units_per_gridline"])
    top_y, bot_y = collapsed[0], collapsed[-1]
    # search a little below the last gridline (line can dip lower) but stay well
    # above the bottom legend swatch
    search_bot = min(bot_y + int(spacing), int(H * 0.85))

    # calibration anchor: prefer the orange setpoint line at a known value;
    # otherwise anchor the top gridline to orp_axis_top.
    anchor_y = anchor_val = None
    setpoint = cfg.get("_orp_setpoint")
    if setpoint is not None:
        oy = [y for y in range(top_y, search_bot + 1)
              if sum(1 for x in range(int(W * 0.2), int(W * 0.85)) if _is_orange(*px[x, y])) > 20]
        if oy:
            anchor_y, anchor_val = statistics.median(oy), float(setpoint)
    if anchor_y is None:
        top_v = cfg.get("orp_axis_top")
        if top_v is None:
            return None, {"error": "no calibration (need setpoint on page or "
                                    "orp_axis_top in config)"}
        anchor_y, anchor_val = top_y, float(top_v)

    def value(y):
        return anchor_val + (anchor_y - y) / px_per_unit

    # rightmost blue column inside the plot = latest reading
    cols = _blue_line_columns(px, W, top_y - 2, search_bot)
    if not cols:
        return None, {"error": "no blue line pixels found", **ocr_note}
    last_x = max(cols)
    return round(value(cols[last_x])), {
        "source": "gridline_fallback", "ocr": ocr_note.get("ocr"),
        "spacing": spacing, "px_per_unit": round(px_per_unit, 3),
        "anchor_y": anchor_y, "anchor_val": anchor_val,
    }


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def scrape_once(cfg, debug=False):
    s = requests.Session()
    s.headers.update({"User-Agent": cfg["user_agent"]})
    login(s, cfg)
    populated = fetch_dashboard(s, cfg)   # follows btnDash redirect -> populated page
    if debug:
        os.makedirs(cfg["debug_dir"], exist_ok=True)
        open(os.path.join(cfg["debug_dir"], "dashboard.html"), "w").write(populated)

    ph = parse_ph(populated, cfg)
    orp_status = parse_orp_status(populated, cfg)
    # config setpoint/axis_top are only the OCR fallback anchor
    cfg["_orp_setpoint"] = cfg.get("orp_setpoint") or cfg.get("orp_axis_top")

    orp = None
    orp_meta = {}
    url = find_orp_chart_url(populated, cfg["base_url"])
    if url:
        png = s.get(url, timeout=30).content
        if debug:
            os.makedirs(cfg["debug_dir"], exist_ok=True)
            open(os.path.join(cfg["debug_dir"], "orp_chart.png"), "wb").write(png)
        orp, orp_meta = orp_from_pixels(png, cfg)
    else:
        orp_meta = {"error": "no ORP chart on page (pump off / not selected?)"}

    # Prefer the setpoint read dynamically off the chart (OCR path); fall back to
    # the configured value only when OCR couldn't provide one.
    setpoint = orp_meta.get("setpoint", cfg.get("orp_setpoint"))

    # No chart AND no pH => the graph is empty (pump off / controller offline).
    # Report it explicitly so HA can hold the last value rather than reading null.
    has_data = (orp is not None) or (ph is not None)
    warnings = parse_warnings(populated)
    return {
        "ph": ph,
        "orp": orp,
        "orp_status": orp_status,
        "orp_setpoint": setpoint,
        "pump_speed": parse_label(populated, "fpStatusLBL"),
        "pump_state": parse_label(populated, "fpStateLBL"),
        "last_orp": parse_label(populated, "lastORPLBL"),
        "last_ph": parse_label(populated, "lastPHLBL"),
        "warnings": warnings,
        "last_warning": warnings[0]["description"] if warnings else None,
        "orp_meta": orp_meta,
        "has_data": has_data,
        "ts": int(time.time()),
    }


def write_json_atomic(path, obj):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)   # don't blow up if the web root isn't there yet
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".pool.", suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(obj, fh, indent=2)
    os.replace(tmp, path)          # atomic; Apache never serves a half-written file
    os.chmod(path, 0o644)


def run(cfg, debug=False):
    last_err = None
    for attempt in range(1, int(cfg["retries"]) + 1):
        try:
            readings = scrape_once(cfg, debug=debug)
            readings["ok"] = True
            write_json_atomic(cfg["json_out"], readings)
            return readings
        except Exception as e:                       # noqa: BLE001
            last_err = str(e)
            print("attempt %d/%d failed: %s" % (attempt, cfg["retries"], e),
                  file=sys.stderr)
            time.sleep(2)
    # keep last good file in place, but record the failure alongside it. Guard
    # the error write too -- if json_out's dir is unwritable, don't traceback.
    err = {"ok": False, "error": last_err, "ts": int(time.time())}
    try:
        write_json_atomic(cfg["json_out"] + ".err", err)
    except OSError as e:
        print("could not write error file: %s" % e, file=sys.stderr)
    return err


def main():
    ap = argparse.ArgumentParser(description="ConnectMyPool -> JSON for Home Assistant")
    ap.add_argument("-c", "--config", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.json"))
    ap.add_argument("--debug", action="store_true",
                    help="save raw dashboard/delta/chart artifacts to debug_dir")
    args = ap.parse_args()
    cfg = load_config(args.config)
    if not cfg["username"] or not cfg["password"]:
        print("ERROR: set username/password in config.json or CMP_USER/CMP_PASS",
              file=sys.stderr)
        sys.exit(2)
    print(json.dumps(run(cfg, debug=args.debug), indent=2))


if __name__ == "__main__":
    main()
