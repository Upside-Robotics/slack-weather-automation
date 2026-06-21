#!/usr/bin/env python3
"""3-hour rain alert: posts to rain-alert Slack channel when rain is coming within 3 hours.

Runs every 15 minutes. Alerts only when the first wet hourly slot falls within the
[now+2h, now+4h] window — roughly "3 hours away" — to avoid spamming on every check.
Heavy rain flag triggers when forecasted precipitation > 5 mm in the rain window.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

# Load .env — check home dir first (EC2), then script dir (local)
for _env_path in [os.path.expanduser("~/.env"), os.path.join(os.path.dirname(__file__), ".env")]:
    if os.path.exists(_env_path):
        with open(_env_path) as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip())
        break

# Rain-alert channel webhook
RAIN_ALERT_WEBHOOK = os.environ.get("RAIN_ALERT_WEBHOOK_URL", "").strip()

# Thresholds
RAIN_PROB_MIN = 50        # % probability to consider an hour "wet"
RAIN_MM_MIN = 0.2         # mm per hour to consider an hour "wet"
HEAVY_MM_THRESHOLD = 5.0  # mm in rain window → "heavy rain"

# Alert window: first wet hour must be at least MIN_HOURS_AHEAD and at most MAX_HOURS_AHEAD
# This prevents re-alerting every 15 min for the same event.
MIN_HOURS_AHEAD = 2.0
MAX_HOURS_AHEAD = 4.0

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

REGION_ORDER = [
    "South", "South Central", "Central", "Arthur", "Mildmay", "West Coast", "North",
]

REGION_FIELDS = {
    "South": ["Martin Gerrits", "Kerrigan", "Sydenham-1 (Bogaert)", "Sydenham-2",
               "Burm", "Scott Campbell", "Wecker", "Bercab Farms"],
    "South Central": ["Roland McAlpine", "Field and Flock (1)", "Field and Flock (2)",
                      "Moosberger Farms", "Peters", "Veldale", "John McRoberts"],
    "Central": ["Benderbrook", "Schumhaven Farms", "Triple Lane Farms",
                "Harrison Farms", "Clair Horst", "Gerber Acres", "Greg Leis"],
    "Arthur": ["Klavan", "Triaro Farms", "Judd / Marvara"],
    "Mildmay": ["Grubb / GerMar Farms", "Schaus / Brad Haack", "Lang Farms",
                "Renwick-1", "Renwick-2"],
    "West Coast": ["Wettlaufer", "Brucelea Poultry"],
    "North": ["Biermans Farms", "Christie-1", "Christie-2", "Highland Farms"],
}

_SHORT = {
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


def _fmt_clock(dt: datetime) -> str:
    h12 = dt.hour % 12 or 12
    ap = "AM" if dt.hour < 12 else "PM"
    if dt.minute:
        return f"{h12}:{dt.minute:02d} {ap}"
    return f"{h12} {ap}"


def fetch_hourly(lat: float, lon: float) -> dict | None:
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&hourly=precipitation,precipitation_probability"
        "&timezone=America%2FToronto"
        "&forecast_days=2"
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                data = json.loads(r.read())
            if data.get("error"):
                return None
            return data
        except Exception as e:
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
            else:
                print(f"  ! {lat},{lon}: {e}")
    return None


def _parse_local_hour(iso: str) -> datetime:
    """Parse Open-Meteo local time like '2026-06-19T14:00' as naive local (Toronto)."""
    s = iso[:16]  # trim to 'YYYY-MM-DDTHH:MM'
    return datetime.fromisoformat(s)


def analyze_field(data: dict, now_local: datetime) -> dict | None:
    """
    Returns rain event info if rain starts in [now+MIN_HOURS_AHEAD, now+MAX_HOURS_AHEAD],
    else None.

    Returns dict with:
      start_dt, end_dt, duration_h, total_mm, peak_mm_per_h, is_heavy
    """
    h = data.get("hourly")
    if not h:
        return None
    times = h.get("time", [])
    prec = h.get("precipitation") or [0] * len(times)
    prob = h.get("precipitation_probability") or [0] * len(times)

    # Build list of (datetime, mm, prob%) for wet hours
    wet_hours: list[tuple[datetime, float, float]] = []
    for i, t in enumerate(times):
        dt = _parse_local_hour(t)
        hours_ahead = (dt - now_local).total_seconds() / 3600
        if hours_ahead < 0:
            continue
        mm = float(prec[i] if i < len(prec) and prec[i] is not None else 0)
        pr = float(prob[i] if i < len(prob) and prob[i] is not None else 0)
        if mm >= RAIN_MM_MIN or pr >= RAIN_PROB_MIN:
            wet_hours.append((dt, mm, pr))

    if not wet_hours:
        return None

    first_dt = wet_hours[0][0]
    hours_until = (first_dt - now_local).total_seconds() / 3600

    # Only alert when rain is within our target window (avoids repeated alerts)
    if not (MIN_HOURS_AHEAD <= hours_until <= MAX_HOURS_AHEAD):
        return None

    # Find contiguous rain window from first wet hour
    rain_run: list[tuple[datetime, float, float]] = [wet_hours[0]]
    for i in range(1, len(wet_hours)):
        prev_dt = rain_run[-1][0]
        curr_dt = wet_hours[i][0]
        if (curr_dt - prev_dt).total_seconds() <= 3600:
            rain_run.append(wet_hours[i])
        else:
            break  # gap → stop at first contiguous block

    end_dt = rain_run[-1][0]
    duration_h = int((end_dt - first_dt).total_seconds() / 3600) + 1
    total_mm = sum(r[1] for r in rain_run)
    peak_mm = max(r[1] for r in rain_run)

    return {
        "start_dt": first_dt,
        "end_dt": end_dt,
        "duration_h": duration_h,
        "total_mm": total_mm,
        "peak_mm_per_h": peak_mm,
        "is_heavy": total_mm >= HEAVY_MM_THRESHOLD,
        "hours_until": hours_until,
    }


def build_alert_blocks(alerts: list[dict], now_local: datetime) -> list[dict]:
    """Build Slack blocks for rain alerts grouped by region."""
    # Group by region
    by_name = {a["name"]: a for a in alerts}
    region_lines: dict[str, list[str]] = {}

    for region in REGION_ORDER:
        field_names = REGION_FIELDS.get(region, [])
        lines = []
        for fn in field_names:
            if fn not in by_name:
                continue
            a = by_name[fn]
            ev = a["event"]
            short = _SHORT.get(fn, fn)
            start_str = _fmt_clock(ev["start_dt"])
            end_str = _fmt_clock(ev["end_dt"])
            dur = ev["duration_h"]
            total_mm = ev["total_mm"]
            hours_until = ev["hours_until"]

            rain_type = "🌧️ *Heavy rain*" if ev["is_heavy"] else "🌧️ Rain"
            mm_str = f"{total_mm:.1f} mm"
            if ev["is_heavy"]:
                mm_str = f"*{total_mm:.1f} mm (heavy)*"

            if dur == 1:
                time_str = f"{start_str}"
                dur_str = "~1 hr"
            else:
                time_str = f"{start_str}–{end_str}"
                dur_str = f"~{dur} hrs"

            lines.append(
                f"• *{short}* — {rain_type} · starts *{time_str}* "
                f"({hours_until:.1f}h away) · {dur_str} · {mm_str}"
            )
        if lines:
            region_lines[region] = lines

    if not region_lines:
        return []

    total_fields = len(alerts)
    heavy_count = sum(1 for a in alerts if a["event"]["is_heavy"])
    now_str = _fmt_clock(now_local)

    header_text = (
        f":rain_cloud: *3-Hour Rain Alert* — {total_fields} field{'s' if total_fields != 1 else ''} "
        f"with rain incoming"
        + (f" · :warning: *{heavy_count} heavy (>5mm)*" if heavy_count else "")
        + f"\n_Checked at {now_str} ET_"
    )

    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header_text}},
        {"type": "divider"},
    ]

    for region in REGION_ORDER:
        if region not in region_lines:
            continue
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{region}*\n" + "\n".join(region_lines[region]),
            },
        })

    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "Source: Open-Meteo · America/Toronto · alerts fire once per rain onset"}],
    })
    return blocks


def post_to_slack(blocks: list[dict]) -> None:
    if not RAIN_ALERT_WEBHOOK:
        print("RAIN_ALERT_WEBHOOK_URL not set; skipping Slack post.")
        return
    payload = json.dumps({"blocks": blocks}).encode("utf-8")
    req = urllib.request.Request(
        RAIN_ALERT_WEBHOOK,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = r.read().decode()
    if resp.strip() != "ok":
        print(f"Slack response: {resp}")


def main() -> None:
    # Use local Toronto time (UTC-4 in EDT, UTC-5 in EST).
    # Open-Meteo returns local times already — we compare naive datetimes.
    # Compute local now by fetching one field's current UTC offset implicitly:
    # easier to just subtract known EDT offset (UTC-4) in summer.
    now_utc = datetime.now(timezone.utc)
    # America/Toronto: EDT = UTC-4 (summer), EST = UTC-5 (winter)
    # Simple DST check: second Sunday in March → first Sunday in November
    # For robustness just use utcoffset from the OS via datetime.now() without tz
    now_local = datetime.now()  # system time; works when system is set to ET or in GH Actions (UTC → adjust)

    # If running in GitHub Actions (UTC), shift to Toronto time manually
    tz_name = time.tzname[0] if time.daylight == 0 else time.tzname[time.daylight]
    if "UTC" in tz_name or "GMT" in tz_name:
        # Determine EDT vs EST: EDT starts 2nd Sun March, ends 1st Sun Nov
        year = now_utc.year
        # 2nd Sunday of March
        mar1 = datetime(year, 3, 1)
        edt_start = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7)
        # 1st Sunday of November
        nov1 = datetime(year, 11, 1)
        est_start = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
        naive_utc = now_utc.replace(tzinfo=None)
        if edt_start <= naive_utc < est_start:
            now_local = naive_utc - timedelta(hours=4)  # EDT
        else:
            now_local = naive_utc - timedelta(hours=5)  # EST

    print(f"Rain alert (3h) — {now_local:%Y-%m-%d %H:%M} ET · {len(FIELDS)} fields")

    alerts: list[dict] = []
    for i, field in enumerate(FIELDS, 1):
        print(f"  [{i:02d}/{len(FIELDS)}] {field['name']}...", end=" ", flush=True)
        data = fetch_hourly(field["lat"], field["lon"])
        if not data:
            print("failed")
            continue
        event = analyze_field(data, now_local)
        if event:
            print(
                f"RAIN in {event['hours_until']:.1f}h · {event['total_mm']:.1f}mm · "
                f"{'HEAVY' if event['is_heavy'] else 'moderate'} · {event['duration_h']}h"
            )
            alerts.append({"name": field["name"], "event": event})
        else:
            print("clear")

    if not alerts:
        print(f"\nNo fields with rain in {MIN_HOURS_AHEAD}–{MAX_HOURS_AHEAD}h window.")
        now_str = _fmt_clock(now_local)
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": (
            f":white_check_mark: *No fields with rain in the 2–4 hour window*\n"
            f"_Checked at {now_str} ET_"
        )}}]
        post_to_slack(blocks)
        print("Posted clear status to Slack.")
        return

    print(f"\n{len(alerts)} field(s) alerting — posting to Slack...")
    blocks = build_alert_blocks(alerts, now_local)
    post_to_slack(blocks)
    print("Posted.")


if __name__ == "__main__":
    main()
