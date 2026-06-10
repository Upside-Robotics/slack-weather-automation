#!/usr/bin/env python3
"""Upside Fields weather → Slack (Open-Meteo + incoming webhook or slash command).

Env: SLACK_WEBHOOK_URL (required for `post`). SLACK_SIGNING_SECRET (recommended for `server`).
Optional: SLACK_SLASH_COMMAND (default /fieldweather).

Slack app: Slash Commands → Create New Command → Command `/fieldweather` → Request URL
`https://<your-host>/slack/command` → save. Reinstall app to the workspace if prompted.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import io
import json
import math
import os
import threading
import time
import urllib.request
from collections import OrderedDict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

# --- config ---

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/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").strip()
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "").strip()
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "").strip()
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "").strip()
SLACK_ALERT_MENTION = os.environ.get("SLACK_ALERT_MENTION", "U0ASGFMA9TQ").strip()  # Alex M
_SLASH = os.environ.get("SLACK_SLASH_COMMAND", "/fieldweather").strip()
SLACK_SLASH_COMMAND = _SLASH if _SLASH.startswith("/") else f"/{_SLASH}"

RAIN_ALERT_THRESHOLD = 50
# Thresholds for the dedicated rain-alert command (stricter to avoid noise)
RAIN_ALERT_PCT = 70
RAIN_ALERT_MM = 2.0
# Hourly windows for “when rain” (slightly looser than daily alert)
HOURLY_WET_PROB_MIN = 40
# Wind speed threshold to highlight (km/h)
WIND_ALERT_KMH = 30

# Nearest place name for lat/lon (Photon reverse geocoder). Cached per run.
_PLACE_CACHE: dict[tuple[float, float], str] = {}


def forecast_reference_place(lat: float, lon: float) -> str:
    """Human label for where the grid-point forecast applies (town/city + region)."""
    key = (round(float(lat), 3), round(float(lon), 3))
    if key in _PLACE_CACHE:
        return _PLACE_CACHE[key]
    q = urlencode({"lat": lat, "lon": lon, "lang": "en"})
    url = f"https://photon.komoot.io/reverse?{q}"
    req = urllib.request.Request(url, headers={"User-Agent": "UpsideFieldsWeather/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        feats = data.get("features") or []
        if not feats:
            raise ValueError("empty")
        p = feats[0].get("properties") or {}
        place = (
            p.get("city")
            or p.get("town")
            or p.get("village")
            or p.get("district")
            or p.get("county")
            or p.get("name")
            or ""
        )
        place = str(place).strip()
        state = str(p.get("state") or "").strip()
        if place and state and state not in place:
            label = f"{place}, {state}"
        elif place:
            label = place
        elif state:
            label = state
        else:
            label = f"{float(lat):.2f}°, {float(lon):.2f}°"
    except Exception:
        label = f"{float(lat):.2f}°, {float(lon):.2f}°"
    _PLACE_CACHE[key] = label
    return label


FIELDS = [
    {"name": "Brucelea Poultry", "lat": 44.035611, "lon": -81.608750},
    {"name": "Renwick-1", "lat": 44.046991, "lon": -81.091850},
    {"name": "Renwick-2", "lat": 43.935306, "lon": -81.198972},
    {"name": "Biermans Farms", "lat": 44.373324, "lon": -81.168756},
    {"name": "Gerber Acres", "lat": 43.524556, "lon": -80.750167},
    {"name": "Peters", "lat": 42.773167, "lon": -80.526472},
    {"name": "Triple Lane Farms", "lat": 43.260583, "lon": -80.263000},
    {"name": "Burm", "lat": 42.548488, "lon": -82.306316},
    {"name": "Greg Leis", "lat": 43.363445, "lon": -80.781518},
    {"name": "Schumhaven Farms", "lat": 43.308778, "lon": -80.783222},
    {"name": "Schaus / Brad Haack", "lat": 44.092140, "lon": -81.025142},
    {"name": "Grubb / GerMar Farms", "lat": 44.072250, "lon": -81.188333},
    {"name": "Martin Gerrits", "lat": 42.958300, "lon": -82.084654},
    {"name": "Field and Flock (1)", "lat": 42.690046, "lon": -80.972857},
    {"name": "Field and Flock (2)", "lat": 42.702233, "lon": -81.111585},
    {"name": "Judd / Marvara", "lat": 43.699806, "lon": -80.664583},
    {"name": "Highland Farms", "lat": 44.213694, "lon": -80.513611},
    {"name": "Triaro Farms", "lat": 43.809315, "lon": -80.539153},
    {"name": "Moosberger Farms", "lat": 42.680472, "lon": -80.895528},
    {"name": "John McRoberts", "lat": 43.199306, "lon": -80.742583},
    {"name": "Harrison Farms", "lat": 43.269992, "lon": -80.589027},
    {"name": "Sydenham-1 (Bogaert)", "lat": 42.645534, "lon": -82.426834},
    {"name": "Sydenham-2", "lat": 42.634574, "lon": -82.472090},
    {"name": "Bercab Farms", "lat": 42.588191, "lon": -82.281917},
    {"name": "Kerrigan", "lat": 42.982695, "lon": -82.142334},
    {"name": "Benderbrook", "lat": 43.320528, "lon": -80.748111},
    {"name": "Lang Farms", "lat": 44.225670, "lon": -81.288647},
    {"name": "Scott Campbell", "lat": 42.444009, "lon": -82.051753},
    {"name": "Christie-1", "lat": 44.443833, "lon": -81.314889},
    {"name": "Christie-2", "lat": 44.451694, "lon": -81.191889},
    {"name": "Klavan", "lat": 43.756753, "lon": -80.574343},
    {"name": "Veldale", "lat": 43.086327, "lon": -80.557516},
    {"name": "Wecker", "lat": 42.335299, "lon": -82.239931},
    {"name": "Clair Horst", "lat": 43.578940, "lon": -80.665490},
    {"name": "Wettlaufer", "lat": 43.806643, "lon": -81.675563},
    {"name": "Roland McAlpine", "lat": 42.772590, "lon": -81.813925},
]

REGION_FIELDS: OrderedDict[str, list[str]] = OrderedDict([
    ("South", [
        "Martin Gerrits", "Kerrigan", "Sydenham-1 (Bogaert)", "Sydenham-2",
        "Burm", "Scott Campbell", "Wecker", "Bercab Farms",
    ]),
    ("South Central", [
        "Roland McAlpine", "Field and Flock (1)", "Field and Flock (2)",
        "Moosberger Farms", "Peters", "Veldale", "John McRoberts",
    ]),
    ("Central", [
        "Benderbrook", "Schumhaven Farms", "Triple Lane Farms",
        "Harrison Farms", "Clair Horst", "Gerber Acres", "Greg Leis",
    ]),
    ("Arthur", ["Klavan", "Triaro Farms", "Judd / Marvara"]),
    ("Mildmay", [
        "Grubb / GerMar Farms", "Schaus / Brad Haack", "Lang Farms",
        "Renwick-1", "Renwick-2",
    ]),
    ("West Coast", ["Wettlaufer", "Brucelea Poultry"]),
    ("North", ["Biermans Farms", "Christie-1", "Christie-2", "Highland Farms"]),
])

_SHORT: dict[str, str] = {
    "Martin Gerrits": "Gerrits", "Kerrigan": "Kerrigan",
    "Sydenham-1 (Bogaert)": "Sydenham-1", "Sydenham-2": "Sydenham-2",
    "Burm": "Burm", "Scott Campbell": "Campbell",
    "Wecker": "Wecker", "Bercab Farms": "Bercab",
    "Roland McAlpine": "McAlpine", "Field and Flock (1)": "F&F (1)",
    "Field and Flock (2)": "F&F (2)", "Moosberger Farms": "Moosberger",
    "Peters": "Peters", "Veldale": "Veldale", "John McRoberts": "McRoberts",
    "Benderbrook": "Benderbrook", "Schumhaven Farms": "Schumhaven",
    "Triple Lane Farms": "Triple Lane", "Harrison Farms": "Harrison",
    "Clair Horst": "Horst", "Gerber Acres": "Gerber", "Greg Leis": "Leis",
    "Klavan": "Klavan", "Triaro Farms": "Triaro", "Judd / Marvara": "Marvara",
    "Grubb / GerMar Farms": "GerMar", "Schaus / Brad Haack": "Schaus",
    "Lang Farms": "Lang", "Renwick-1": "Renwick-1", "Renwick-2": "Renwick-2",
    "Wettlaufer": "Wettlaufer", "Brucelea Poultry": "Brucelea",
    "Biermans Farms": "Biermans", "Christie-1": "Christie-1",
    "Christie-2": "Christie-2", "Highland Farms": "Highland",
}

WMO = {
    0: ("Clear sky", "☀️"),
    1: ("Mainly clear", "🌤️"),
    2: ("Partly cloudy", "⛅"),
    3: ("Overcast", "☁️"),
    45: ("Foggy", "🌫️"),
    48: ("Icy fog", "🌫️"),
    51: ("Light drizzle", "🌦️"),
    53: ("Drizzle", "🌦️"),
    55: ("Heavy drizzle", "🌧️"),
    61: ("Light rain", "🌧️"),
    63: ("Rain", "🌧️"),
    65: ("Heavy rain", "🌧️"),
    71: ("Light snow", "🌨️"),
    73: ("Snow", "❄️"),
    75: ("Heavy snow", "❄️"),
    77: ("Snow grains", "🌨️"),
    80: ("Light showers", "🌦️"),
    81: ("Showers", "🌧️"),
    82: ("Heavy showers", "⛈️"),
    85: ("Snow showers", "🌨️"),
    86: ("Heavy snow showers", "❄️"),
    95: ("Thunderstorm", "⛈️"),
    96: ("Thunderstorm + hail", "⛈️"),
    99: ("Thunderstorm + hail", "⛈️"),
}


def wmo_label(code):
    if code is None:
        return ("Unknown", "❓")
    return WMO.get(int(code), (f"Code {code}", "🌡️"))


# --- image generation ---

_CELL_W = 112
_HDR_H = 32
_NAME_H = 36
_ICON_H = 72
_ALERT_H = 42
_C_BG = (22, 22, 22)
_C_GRID = (52, 52, 52)
_C_HDRBAR = (38, 38, 38)
_C_TEXT = (222, 222, 222)
_C_DIM = (130, 130, 130)


def _pil_font(size: int):
    try:
        from PIL import ImageFont
        for path in [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
        ]:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
        return ImageFont.load_default()
    except Exception:
        return None


def _txt(draw, text: str, bx: int, by: int, bw: int, bh: int, font, color):
    cx, cy = bx + bw // 2, by + bh // 2
    try:
        draw.text((cx, cy), text, font=font, fill=color, anchor="mm")
    except Exception:
        draw.text((bx + 4, by + bh // 2 - 6), text, fill=color)


def _sun(draw, x, y, w, h):
    cx, cy, r = x + w // 2, y + h // 2, min(w, h) // 3
    for i in range(8):
        a = i * math.pi / 4
        draw.line(
            [cx + int((r + 2) * math.cos(a)), cy + int((r + 2) * math.sin(a)),
             cx + int((r + 9) * math.cos(a)), cy + int((r + 9) * math.sin(a))],
            fill=(255, 200, 0), width=2,
        )
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 200, 0))


def _cloud(draw, x, y, w, h, col=(108, 108, 112)):
    px = lambda f: x + int(w * f)
    py = lambda f: y + int(h * f)
    draw.ellipse([px(.05), py(.30), px(.45), py(.90)], fill=col)
    draw.ellipse([px(.25), py(.10), px(.72), py(.70)], fill=col)
    draw.ellipse([px(.52), py(.30), px(.95), py(.90)], fill=col)
    draw.rectangle([px(.05), py(.55), px(.95), py(.90)], fill=col)


def _drops(draw, x, y, w, h, n=3, col=(65, 128, 210)):
    step = w // (n + 1)
    for i in range(n):
        rx = x + step * (i + 1)
        draw.line([rx, y + 2, rx - 5, y + h - 2], fill=col, width=2)


def _bolt(draw, x, y, w, h):
    cx = x + w // 2
    pts = [(cx + 6, y), (cx - 4, y + h // 2), (cx + 5, y + h // 2), (cx - 5, y + h)]
    draw.polygon(pts, fill=(255, 220, 30))


def _snow_dots(draw, x, y, w, h, n=4):
    step = w // (n + 1)
    for i in range(n):
        sx, sy, r = x + step * (i + 1), y + h // 2, 4
        draw.ellipse([sx - r, sy - r, sx + r, sy + r], fill=(185, 215, 255))


def _weather_icon(draw, x, y, w, h, wmo_code: int):
    p = 6
    ch = int(h * 0.56)
    bx, by = x + p, y + p
    bw, bh = w - 2 * p, h - 2 * p
    if wmo_code == 0:
        _sun(draw, bx, by, bw, bh)
    elif wmo_code in (1, 2):
        _sun(draw, bx, by, bw * 2 // 3, bh * 2 // 3)
        _cloud(draw, x + w // 5, y + h // 4, w - w // 5 - p, h - h // 4 - p, col=(125, 125, 128))
    elif wmo_code == 3:
        _cloud(draw, bx, by, bw, bh, col=(88, 88, 92))
    elif wmo_code in (45, 48):
        for i in range(3):
            fy = by + i * bh // 3
            draw.rectangle([bx, fy, bx + bw, fy + 4], fill=(155, 155, 155))
    elif wmo_code in (51, 53, 55):
        _cloud(draw, bx, by, bw, ch - p)
        _drops(draw, bx, y + ch, bw, h - ch - p, n=2, col=(100, 155, 220))
    elif wmo_code in (61, 63, 65, 80, 81, 82):
        _cloud(draw, bx, by, bw, ch - p)
        _drops(draw, bx, y + ch, bw, h - ch - p)
    elif wmo_code in (95, 96, 99):
        _cloud(draw, bx, by, bw, ch - p, col=(68, 68, 72))
        mid = bw // 3
        _bolt(draw, bx + bw // 2 - mid // 2, y + ch, mid, (h - ch) // 2)
        _drops(draw, bx, y + ch + (h - ch) // 2, bw, (h - ch) // 2 - p, n=2)
    elif wmo_code in (71, 73, 75, 77, 85, 86):
        _cloud(draw, bx, by, bw, ch - p)
        _snow_dots(draw, bx, y + ch, bw, h - ch - p)


def _alert_cell(draw, x, y, w, h, frost_c, is_failed: bool):
    if is_failed:
        cx, cy, r = x + w // 2, y + h // 2, min(w, h) // 5
        draw.line([cx - r, cy - r, cx + r, cy + r], fill=(90, 90, 90), width=2)
        draw.line([cx + r, cy - r, cx - r, cy + r], fill=(90, 90, 90), width=2)
        return
    if frost_c is not None and frost_c <= FROST_ALERT_C:
        cx = x + w // 2
        th = min(w, h) * 3 // 4
        ty = y + (h - th) // 2
        pts = [(cx, ty), (cx - th // 2, ty + th), (cx + th // 2, ty + th)]
        draw.polygon(pts, fill=(28, 78, 200))
        draw.rectangle([cx - 1, ty + 6, cx + 1, ty + th * 2 // 3], fill=(255, 255, 255))
        draw.ellipse([cx - 2, ty + th * 2 // 3 + 2, cx + 2, ty + th * 2 // 3 + 6], fill=(255, 255, 255))


def generate_region_image(region_name: str, field_results: list) -> bytes | None:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    n = len(field_results)
    if n == 0:
        return None
    img_w = n * _CELL_W
    img_h = _HDR_H + _NAME_H + _ICON_H + _ALERT_H
    img = Image.new("RGB", (img_w, img_h), _C_BG)
    draw = ImageDraw.Draw(img)
    fhdr = _pil_font(15)
    fname = _pil_font(10)

    draw.rectangle([0, 0, img_w, _HDR_H], fill=_C_HDRBAR)
    _txt(draw, region_name, 0, 0, img_w, _HDR_H, fhdr, _C_TEXT)
    draw.line([0, _HDR_H, img_w, _HDR_H], fill=_C_GRID)
    draw.line([0, _HDR_H + _NAME_H, img_w, _HDR_H + _NAME_H], fill=_C_GRID)
    draw.line([0, _HDR_H + _NAME_H + _ICON_H, img_w, _HDR_H + _NAME_H + _ICON_H], fill=_C_GRID)

    for i, r in enumerate(field_results):
        cx = i * _CELL_W
        if i > 0:
            draw.line([cx, _HDR_H, cx, img_h], fill=_C_GRID)
        wx = r.get("wx")
        name = _SHORT.get(r["name"], r["name"])
        _txt(draw, name, cx, _HDR_H, _CELL_W, _NAME_H, fname, _C_DIM if not wx else _C_TEXT)
        if wx and wx.get("wmo") is not None:
            _weather_icon(draw, cx + 4, _HDR_H + _NAME_H + 4, _CELL_W - 8, _ICON_H - 8, int(wx["wmo"]))
        sf = (wx or {}).get("snow_frost") or {}
        frost_c = sf.get("low_c") if sf.get("frost_risk") else None
        _alert_cell(draw, cx + 4, _HDR_H + _NAME_H + _ICON_H + 4, _CELL_W - 8, _ALERT_H - 8, frost_c, not wx)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# --- Slack image upload ---

def _slack_upload_image(image_bytes: bytes, region_name: str) -> bool:
    slug = region_name.lower().replace(" ", "_")
    params = urlencode({"filename": f"weather_{slug}.png", "length": len(image_bytes)})
    req = urllib.request.Request(
        f"https://slack.com/api/files.getUploadURLExternal?{params}",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except Exception as e:
        print(f"getUploadURLExternal: {e}")
        return False
    if not data.get("ok"):
        print(f"getUploadURLExternal error: {data.get('error')}")
        return False
    put_req = urllib.request.Request(
        data["upload_url"], data=image_bytes,
        headers={"Content-Type": "image/png"}, method="PUT",
    )
    try:
        with urllib.request.urlopen(put_req, timeout=30):
            pass
    except Exception as e:
        print(f"image PUT: {e}")
        return False
    body = json.dumps({
        "files": [{"id": data["file_id"], "title": f"{region_name} Weather"}],
        "channel_id": SLACK_CHANNEL_ID,
    }).encode()
    comp_req = urllib.request.Request(
        "https://slack.com/api/files.completeUploadExternal", data=body,
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(comp_req, timeout=15) as r:
            result = json.loads(r.read())
    except Exception as e:
        print(f"completeUploadExternal: {e}")
        return False
    if not result.get("ok"):
        print(f"completeUploadExternal error: {result.get('error')}")
        return False
    return True


def post_region_images(results: list):
    by_name = {r["name"]: r for r in results}
    for region_name, field_names in REGION_FIELDS.items():
        region_results = [by_name[fn] for fn in field_names if fn in by_name]
        if not region_results:
            continue
        print(f"  Image: {region_name}...", end=" ", flush=True)
        img_bytes = generate_region_image(region_name, region_results)
        if not img_bytes:
            print("PIL unavailable")
            continue
        ok = _slack_upload_image(img_bytes, region_name)
        print("ok" if ok else "failed")


def _format_report_date(d):
    return f"{d.strftime('%A, %B ')}{d.day}{d.strftime(' %Y')}"


def _parse_hour_ts(iso_local: str) -> datetime:
    """Open-Meteo local times look like 2026-05-05T14:00."""
    s = iso_local.replace("Z", "+00:00")
    if len(s) == 16:
        return datetime.fromisoformat(s)
    return datetime.fromisoformat(s[:19])


def _fmt_clock(d: datetime) -> str:
    h12 = d.hour % 12
    if h12 == 0:
        h12 = 12
    ap = "AM" if d.hour < 12 else "PM"
    if d.minute:
        return f"{h12}:{d.minute:02d} {ap}"
    return f"{h12} {ap}"


def _rain_timing_details(data, today_date: str, daily_precip_mm):
    """Summarize when rain is likely today from hourly series (America/Toronto)."""
    h = data.get("hourly") if data else None
    if not h or "time" not in h:
        return {
            "ok": False,
            "summary": "Hourly timing not available.",
            "windows": [],
            "peak_mm": None,
            "peak_mm_time": None,
            "peak_prob": None,
            "peak_prob_time": None,
        }
    times = h["time"]
    n = len(times)
    prec = h.get("precipitation") or [0] * n
    prob = h.get("precipitation_probability") or [0] * n
    hours = []
    for i, t in enumerate(times):
        if len(t) < 10 or t[:10] != today_date:
            continue
        p = prec[i] if i < len(prec) and prec[i] is not None else 0.0
        pr = prob[i] if i < len(prob) and prob[i] is not None else 0.0
        hours.append({"t": t, "p": float(p), "pr": float(pr)})
    if not hours:
        return {
            "ok": False,
            "summary": "No hourly rows for today.",
            "windows": [],
            "peak_mm": None,
            "peak_mm_time": None,
            "peak_prob": None,
            "peak_prob_time": None,
        }

    wet = [
        i
        for i, row in enumerate(hours)
        if row["p"] >= 0.1 or row["pr"] >= HOURLY_WET_PROB_MIN
    ]
    if not wet:
        dp = daily_precip_mm if daily_precip_mm is not None else 0.0
        if dp and dp >= 0.2:
            summary = "Light / scattered in the model (no clear hourly peak)."
        else:
            summary = "No meaningful rain in the hourly outlook."
        peak_mm, peak_mm_time = None, None
        peak_prob, peak_prob_time = None, None
        for row in hours:
            if peak_mm is None or row["p"] > peak_mm:
                peak_mm, peak_mm_time = row["p"], row["t"]
            if peak_prob is None or row["pr"] > peak_prob:
                peak_prob, peak_prob_time = row["pr"], row["t"]
        return {
            "ok": True,
            "summary": summary,
            "windows": [],
            "peak_mm": peak_mm,
            "peak_mm_time": peak_mm_time,
            "peak_prob": peak_prob,
            "peak_prob_time": peak_prob_time,
        }

    runs = []
    s = wet[0]
    prev = wet[0]
    for idx in wet[1:]:
        if idx == prev + 1:
            prev = idx
        else:
            runs.append((s, prev))
            s = prev = idx
    runs.append((s, prev))

    parts = []
    for a, b in runs:
        t0 = _parse_hour_ts(hours[a]["t"])
        t1 = _parse_hour_ts(hours[b]["t"])
        if a == b:
            parts.append(_fmt_clock(t0))
        else:
            parts.append(f"{_fmt_clock(t0)}–{_fmt_clock(t1)}")
    peak_mm, peak_mm_time = None, None
    peak_prob, peak_prob_time = None, None
    for row in hours:
        if peak_mm is None or row["p"] > peak_mm:
            peak_mm, peak_mm_time = row["p"], row["t"]
        if peak_prob is None or row["pr"] > peak_prob:
            peak_prob, peak_prob_time = row["pr"], row["t"]
    return {
        "ok": True,
        "summary": ", ".join(parts),
        "windows": parts,
        "peak_mm": peak_mm,
        "peak_mm_time": peak_mm_time,
        "peak_prob": peak_prob,
        "peak_prob_time": peak_prob_time,
    }



def _temp_badge(max_temp, min_temp) -> str:
    """🔴/🔵 dots scaled to how extreme the day's high or low is."""
    try:
        hi = float(max_temp)
        if hi >= 36:
            return " 🔴🔴🔴"
        if hi >= 32:
            return " 🔴🔴"
        if hi >= 28:
            return " 🔴"
    except (TypeError, ValueError):
        pass
    try:
        lo = float(min_temp)
        if lo <= -20:
            return " 🔵🔵🔵"
        if lo <= -10:
            return " 🔵🔵"
        if lo <= -5:
            return " 🔵"
    except (TypeError, ValueError):
        pass
    return ""


def _wind_alert_text(wx) -> str:
    w = wx.get("wind_kmh")
    try:
        w = float(w)
    except (TypeError, ValueError):
        return ""
    if w < WIND_ALERT_KMH:
        return ""
    return f" · *Wind:* {w:.0f} km/h"


def _uv_label(uv: float) -> str:
    if uv < 3:
        return "Low"
    if uv < 6:
        return "Moderate"
    if uv < 8:
        return "High"
    if uv < 11:
        return "Very High"
    return "Extreme"


_RAIN_WMO = {51, 53, 55, 61, 63, 65, 80, 81, 82, 95, 96, 99}


def _is_currently_raining(wx) -> tuple[bool, float]:
    curr = wx.get("current")
    if not curr:
        return False, 0.0
    try:
        p = float(curr.get("precipitation") or 0)
    except (TypeError, ValueError):
        p = 0.0
    wmo = curr.get("weather_code")
    is_raining = p > 0 or (wmo is not None and int(wmo) in _RAIN_WMO)
    return is_raining, p


def _current_rain_str(wx) -> str:
    curr = wx.get("current")
    if not curr:
        return ""
    is_raining, p = _is_currently_raining(wx)
    if is_raining:
        mention = f"<@{SLACK_ALERT_MENTION}> " if SLACK_ALERT_MENTION else ""
        return f" · 🌧️ *now {p:.1f}mm* {mention}"
    return " · now dry"


def _heat_info_text(wx) -> str:
    try:
        hi = float(wx["max_temp"])
    except (TypeError, ValueError):
        return ""
    if hi < 28:
        return ""
    parts = []
    if wx.get("heat_wave"):
        parts.append("🔥 *Heat wave*")
    apparent = wx.get("apparent_max")
    try:
        parts.append(f"feels like {float(apparent):.0f}°C")
    except (TypeError, ValueError):
        pass
    uv = wx.get("uv_max")
    try:
        uv_val = float(uv)
        parts.append(f"UV {uv_val:.0f} ({_uv_label(uv_val)})")
    except (TypeError, ValueError):
        pass
    return (" · " + ", ".join(parts)) if parts else ""


def fetch_weather(lat, lon):
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&daily=temperature_2m_max,temperature_2m_min,"
        "precipitation_sum,precipitation_probability_max,"
        "weathercode,windspeed_10m_max,"
        "apparent_temperature_max,uv_index_max"
        "&hourly=precipitation,precipitation_probability"
        "&current=precipitation,rain,weather_code"
        "&timezone=America%2FToronto"
        "&forecast_days=3"
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                data = json.loads(r.read())
            if data.get("error"):
                print(f"  ! {lat},{lon}: API error — {data.get('reason', data)}")
                return None
            return data
        except Exception as e:
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
            else:
                print(f"  ! {lat},{lon}: {e}")
    return None


def parse_today(data):
    if not data or "daily" not in data:
        return None
    d = data["daily"]
    try:
        today = d["time"][0]
        precip_mm = d["precipitation_sum"][0]
        rain_timing = _rain_timing_details(data, today, precip_mm)
        highs = d["temperature_2m_max"]
        heat_wave = sum(1 for t in highs[:3] if t is not None and float(t) >= 32) >= 2
        apparent_max = (d.get("apparent_temperature_max") or [None])[0]
        uv_max = (d.get("uv_index_max") or [None])[0]
        curr = data.get("current") or {}
        current = {
            "precipitation": curr.get("precipitation"),
            "rain": curr.get("rain"),
            "weather_code": curr.get("weather_code"),
        } if curr else None
        return {
            "date": today,
            "max_temp": highs[0],
            "min_temp": d["temperature_2m_min"][0],
            "precip_mm": precip_mm,
            "rain_pct": d["precipitation_probability_max"][0],
            "wmo": d["weathercode"][0],
            "wind_kmh": d["windspeed_10m_max"][0],
            "rain_timing": rain_timing,
            "apparent_max": apparent_max,
            "uv_max": uv_max,
            "heat_wave": heat_wave,
            "current": current,
            "tmr_rain": d["precipitation_probability_max"][1]
            if len(d["precipitation_probability_max"]) > 1
            else None,
            "tmr_precip": d["precipitation_sum"][1] if len(d["precipitation_sum"]) > 1 else None,
            "tmr_wmo": d["weathercode"][1] if len(d["weathercode"]) > 1 else None,
        }
    except (IndexError, KeyError):
        return None


def collect_results(verbose=True):
    results = []
    for i, field in enumerate(FIELDS, 1):
        near = forecast_reference_place(field["lat"], field["lon"])
        if verbose:
            print(f"  [{i:02d}/{len(FIELDS)}] {field['name']} ({near})...", end=" ", flush=True)
        data = fetch_weather(field["lat"], field["lon"])
        wx = parse_today(data)
        if verbose:
            if wx:
                rt = wx.get("rain_timing")
                if isinstance(rt, dict):
                    t = (rt.get("summary") or "")[:60]
                else:
                    t = (rt or "")[:60]
                print(f"{wx['rain_pct']}% · {wx['max_temp']}°C · {t}")
            else:
                print("failed")
        results.append({"name": field["name"], "wx": wx, "near": near})
    return results


def build_slack_blocks(results):
    wx_date = next((r["wx"]["date"] for r in results if r.get("wx")), None)
    today_str = (
        _format_report_date(datetime.strptime(wx_date, "%Y-%m-%d"))
        if wx_date else _format_report_date(datetime.now())
    )
    by_name = {r["name"]: r for r in results}

    def _rain_alert(wx) -> bool:
        return (
            wx is not None
            and wx["rain_pct"] is not None
            and wx["rain_pct"] >= RAIN_ALERT_THRESHOLD
            and float(wx["precip_mm"] or 0) >= 1.0
        )

    total_rain = sum(1 for r in results if _rain_alert(r["wx"]))
    total_frost = sum(
        1 for r in results
        if r["wx"] and isinstance(r["wx"].get("snow_frost"), dict)
        and (r["wx"]["snow_frost"].get("frost_risk") or r["wx"]["snow_frost"].get("snow_signal"))
    )
    total_failed = sum(1 for r in results if not r["wx"])

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "Upside Fields — Daily Weather"}},
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": (
                f"*{today_str}* · {len(results)} fields · "
                f"{total_rain} rain ≥{RAIN_ALERT_THRESHOLD}% · {total_frost} frost/snow"
                + (f" · {total_failed} failed" if total_failed else "")
            )}],
        },
        {"type": "divider"},
    ]

    for region_name, field_names in REGION_FIELDS.items():
        region_results = [by_name[fn] for fn in field_names if fn in by_name]
        if not region_results:
            continue

        rain_n = sum(1 for r in region_results if _rain_alert(r["wx"]))
        frost_n = sum(
            1 for r in region_results
            if r["wx"] and isinstance(r["wx"].get("snow_frost"), dict)
            and (r["wx"]["snow_frost"].get("frost_risk") or r["wx"]["snow_frost"].get("snow_signal"))
        )
        badges = (["🌧️ " + str(rain_n)] if rain_n else []) + (["🧊 " + str(frost_n)] if frost_n else [])
        hdr = f"*{region_name}*  ·  {len(region_results)} fields"
        if badges:
            hdr += "  ·  " + "  ".join(badges)
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": hdr}})

        lines = []
        for r in region_results:
            wx = r["wx"]
            short = _SHORT.get(r["name"], r["name"])
            if not wx:
                lines.append(f"❓ *{short}* · _failed_")
                continue
            condition, emoji = wmo_label(wx["wmo"])
            rain_pct = wx["rain_pct"] if wx["rain_pct"] is not None else 0
            precip = float(wx["precip_mm"] or 0)
            sf = wx.get("snow_frost") or {}
            alert = _rain_alert(wx)
            rain_str = f"*{rain_pct}% / {precip:.1f}mm*" if alert else f"{rain_pct}% / {precip:.1f}mm"
            frost_str = ""
            if sf.get("frost_risk"):
                lc = sf.get("low_c")
                frost_str = f" · 🧊 {lc:.1f}°C" if lc is not None else " · 🧊"
            wind_str = _wind_alert_text(wx)
            temp_str = _temp_badge(wx["max_temp"], wx["min_temp"])
            rt = wx.get("rain_timing")
            when_str = ""
            if isinstance(rt, dict) and rt.get("summary") and rt["summary"] not in (
                "No meaningful rain in the hourly outlook.",
                "Light / scattered in the model (no clear hourly peak).",
            ):
                when_str = f" · {rt['summary']}"
                peak_mm = rt.get("peak_mm")
                peak_mm_time = rt.get("peak_mm_time")
                if peak_mm is not None and peak_mm_time and float(peak_mm) >= 0.1:
                    when_str += f" (peak {_fmt_clock(_parse_hour_ts(peak_mm_time))}, {float(peak_mm):.1f}mm)"
            heat_str = _heat_info_text(wx)
            now_str = _current_rain_str(wx) if alert else ""
            lines.append(
                f"{emoji} *{short}* · {condition} · {wx['max_temp']}°/{wx['min_temp']}°C{temp_str}{heat_str} · {rain_str}{when_str}{now_str}{frost_str}{wind_str}"
            )

        chunk: list[str] = []
        for line in lines:
            chunk.append(line)
            if len(chunk) == 8:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(chunk)}})
                chunk = []
        if chunk:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(chunk)}})

        blocks.append({"type": "divider"})

    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "Open-Meteo · Photon · America/Toronto"}],
    })
    return blocks


def _post_json(url, payload, timeout=120):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()


def post_to_slack(blocks):
    if not SLACK_WEBHOOK_URL:
        raise SystemExit("Set SLACK_WEBHOOK_URL for post (or use: server + slash command).")
    _post_json(SLACK_WEBHOOK_URL, {"blocks": blocks})
    print("Posted to Slack (webhook).")


def post_slash_followup(response_url, blocks):
    _post_json(
        response_url,
        {
            "response_type": "in_channel",
            "blocks": blocks,
        },
    )


def verify_slack_signature(signing_secret, body: str, headers) -> bool:
    if not signing_secret:
        return True
    ts = headers.get("X-Slack-Request-Timestamp")
    sig = headers.get("X-Slack-Signature")
    if not ts or not sig:
        return False
    try:
        if abs(time.time() - int(ts)) > 60 * 5:
            return False
    except ValueError:
        return False
    basestring = f"v0:{ts}:{body}".encode("utf-8")
    want = "v0=" + hmac.new(
        signing_secret.encode("utf-8"),
        basestring,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(want, sig)


def _form_val(form, key):
    v = form.get(key, [""])
    return v[0] if v else ""


def slash_followup_worker(response_url):
    try:
        results = collect_results(verbose=False)
        blocks = build_slack_blocks(results)
        post_slash_followup(response_url, blocks)
    except Exception as e:
        try:
            _post_json(
                response_url,
                {"response_type": "ephemeral", "text": f"Weather bot error: {e}"},
            )
        except Exception:
            pass


def make_handler():
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            print(f"[{self.address_string()}] {fmt % args}")

        def do_GET(self):
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path in ("", "/"):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"ok\n")
                return
            self.send_error(404)

        def do_POST(self):
            path = urlparse(self.path).path.rstrip("/")
            if path != "/slack/command":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            if not verify_slack_signature(SLACK_SIGNING_SECRET, body, self.headers):
                self.send_response(401)
                self.end_headers()
                return
            form = parse_qs(body, keep_blank_values=True)
            cmd = _form_val(form, "command")
            if cmd != SLACK_SLASH_COMMAND:
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                msg = (
                    f"This endpoint is for *{SLACK_SLASH_COMMAND}* only "
                    f"(got `{cmd or '(missing)'}`)."
                )
                self.wfile.write(
                    json.dumps({"response_type": "ephemeral", "text": msg}).encode("utf-8")
                )
                return
            response_url = _form_val(form, "response_url")
            if not response_url:
                self.send_response(400)
                self.end_headers()
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            ack = json.dumps(
                {"response_type": "ephemeral", "text": "Fetching weather for all fields…"}
            )
            self.wfile.write(ack.encode("utf-8"))

            threading.Thread(
                target=slash_followup_worker,
                args=(response_url,),
                daemon=True,
            ).start()

    return H


def cmd_post(_args):
    print(f"Weather bot — {datetime.now():%Y-%m-%d %H:%M} · {len(FIELDS)} fields\n")
    results = collect_results(verbose=True)
    print("\nPosting summary…")
    post_to_slack(build_slack_blocks(results))
    if SLACK_BOT_TOKEN and SLACK_CHANNEL_ID:
        print("Uploading region images…")
        post_region_images(results)
    else:
        print("(Set SLACK_BOT_TOKEN + SLACK_CHANNEL_ID to enable region table images)")


def cmd_dry_run(_args):
    print(f"Dry run — {datetime.now():%Y-%m-%d %H:%M}\n")
    results = collect_results(verbose=True)
    rain = sum(
        1
        for r in results
        if r["wx"] and r["wx"]["rain_pct"] is not None and r["wx"]["rain_pct"] >= RAIN_ALERT_THRESHOLD
    )
    print(f"\nSummary: {rain} high rain risk, {len(FIELDS) - rain - sum(1 for r in results if not r['wx'])} low, "
          f"{sum(1 for r in results if not r['wx'])} failed")


def _mention_str() -> str:
    if not SLACK_ALERT_MENTION:
        return ""
    m = SLACK_ALERT_MENTION.lstrip("@")
    if m in ("here", "channel"):
        return f"<!{m}> "
    return f"<@{SLACK_ALERT_MENTION}> "


def cmd_rain_alert(_args):
    print(f"Rain alert check — {datetime.now():%Y-%m-%d %H:%M}\n")
    results = collect_results(verbose=False)
    rainy = [
        r for r in results
        if r["wx"]
        and r["wx"]["rain_pct"] is not None
        and r["wx"]["rain_pct"] >= RAIN_ALERT_PCT
        and float(r["wx"]["precip_mm"] or 0) >= RAIN_ALERT_MM
    ]
    if not rainy:
        print(f"No fields exceed {RAIN_ALERT_PCT}% / {RAIN_ALERT_MM}mm threshold. No alert sent.")
        return

    mention = _mention_str()
    header = f"{mention}:rain_cloud: *Rain Alert* — {len(rainy)} field{'s' if len(rainy) != 1 else ''} with significant rain forecast"
    lines = []
    for r in rainy:
        wx = r["wx"]
        short = _SHORT.get(r["name"], r["name"])
        rain_pct = wx["rain_pct"]
        precip = float(wx["precip_mm"] or 0)
        rt = wx.get("rain_timing")
        when_str = ""
        if isinstance(rt, dict) and rt.get("summary") and rt["summary"] not in (
            "No meaningful rain in the hourly outlook.",
            "Light / scattered in the model (no clear hourly peak).",
        ):
            when_str = f" · {rt['summary']}"
        lines.append(f"• *{short}* — {rain_pct}% / {precip:.1f}mm{when_str}")

    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": header + "\n" + "\n".join(lines)}}]

    if not SLACK_WEBHOOK_URL:
        print(header)
        print("\n".join(lines))
        return
    _post_json(SLACK_WEBHOOK_URL, {"blocks": blocks})
    print(f"Rain alert posted — {len(rainy)} fields.")


def cmd_server(args):
    if not SLACK_SIGNING_SECRET:
        print("Warning: SLACK_SIGNING_SECRET not set; requests are not verified.")
    Handler = make_handler()
    httpd = HTTPServer((args.host, args.port), Handler)
    print(f"Slash command: {SLACK_SLASH_COMMAND} → http://{args.host}:{args.port}/slack/command")
    print("In Slack app, set that path as the command’s Request URL (HTTPS in prod, e.g. ngrok).")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


def main():
    p = argparse.ArgumentParser(description="Field weather → Slack")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("post", help="Fetch and post via incoming webhook (default)").set_defaults(
        func=cmd_post
    )
    sub.add_parser("dry-run", help="Fetch only, no Slack").set_defaults(func=cmd_dry_run)
    sub.add_parser("rain-alert", help="Post alert if any field has significant rain forecast").set_defaults(func=cmd_rain_alert)
    sp = sub.add_parser("server", help="HTTP server for Slack slash command")
    sp.add_argument("--host", default=os.environ.get("BIND_HOST", "0.0.0.0"))
    sp.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    sp.set_defaults(func=cmd_server)

    args = p.parse_args()
    if args.command is None:
        cmd_post(args)
    else:
        args.func(args)


if __name__ == "__main__":
    main()
