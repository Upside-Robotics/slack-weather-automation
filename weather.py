#!/usr/bin/env python3
"""Upside Fields weather → Slack (Open-Meteo + incoming webhook or slash command).

Env: SLACK_WEBHOOK_URL (required for `post`). SLACK_SIGNING_SECRET (recommended for `server`).
Slash command: point Request URL to https://<host>/slack/command (HTTPS in prod, e.g. ngrok).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

# --- config ---

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "").strip()

RAIN_ALERT_THRESHOLD = 50

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


def _format_report_date(d):
    return f"{d.strftime('%A, %B ')}{d.day}{d.strftime(' %Y')}"


def fetch_weather(lat, lon):
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&daily=temperature_2m_max,temperature_2m_min,"
        "precipitation_sum,precipitation_probability_max,"
        "weathercode,windspeed_10m_max"
        "&timezone=America%2FToronto"
        "&forecast_days=3"
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  ! {lat},{lon}: {e}")
        return None


def parse_today(data):
    if not data or "daily" not in data:
        return None
    d = data["daily"]
    try:
        return {
            "date": d["time"][0],
            "max_temp": d["temperature_2m_max"][0],
            "min_temp": d["temperature_2m_min"][0],
            "precip_mm": d["precipitation_sum"][0],
            "rain_pct": d["precipitation_probability_max"][0],
            "wmo": d["weathercode"][0],
            "wind_kmh": d["windspeed_10m_max"][0],
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
        if verbose:
            print(f"  [{i:02d}/{len(FIELDS)}] {field['name']}...", end=" ", flush=True)
        data = fetch_weather(field["lat"], field["lon"])
        wx = parse_today(data)
        if verbose:
            if wx:
                print(f"{wx['rain_pct']}% rain, {wx['max_temp']}°C")
            else:
                print("failed")
        results.append({"name": field["name"], "wx": wx})
    return results


def build_slack_blocks(results):
    today_str = _format_report_date(datetime.now())

    rain_fields = [
        r
        for r in results
        if r["wx"] and r["wx"]["rain_pct"] is not None and r["wx"]["rain_pct"] >= RAIN_ALERT_THRESHOLD
    ]
    clear_fields = [
        r
        for r in results
        if r["wx"] and (r["wx"]["rain_pct"] is None or r["wx"]["rain_pct"] < RAIN_ALERT_THRESHOLD)
    ]
    error_fields = [r for r in results if not r["wx"]]

    rain_fields.sort(key=lambda x: x["wx"]["rain_pct"], reverse=True)

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Upside Fields — Daily Weather"},
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"*{today_str}* · {len(results)} fields · "
                        f"{len(rain_fields)} rain risk ≥{RAIN_ALERT_THRESHOLD}%"
                    ),
                }
            ],
        },
        {"type": "divider"},
    ]

    if rain_fields:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Rain risk today ({len(rain_fields)})*"},
            }
        )
        for r in rain_fields:
            wx = r["wx"]
            desc, emoji = wmo_label(wx["wmo"])
            _, tmr_emoji = wmo_label(wx.get("tmr_wmo"))
            tmr_rain = wx.get("tmr_rain")
            tmr_str = f" · Tomorrow: {tmr_emoji} {tmr_rain}%" if tmr_rain is not None else ""
            precip = wx["precip_mm"] or 0
            line = (
                f"*{emoji} {r['name']}*\n"
                f"{wx['rain_pct']}% rain · {precip:.1f} mm · "
                f"{wx['max_temp']}° / {wx['min_temp']}°C · {wx['wind_kmh']} km/h · {desc}{tmr_str}"
            )
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": line}})
        blocks.append({"type": "divider"})

    if clear_fields:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Low rain risk ({len(clear_fields)})*"},
            }
        )
        lines = []
        for r in clear_fields:
            wx = r["wx"]
            _, emoji = wmo_label(wx["wmo"])
            rain_pct = wx["rain_pct"] if wx["rain_pct"] is not None else 0
            lines.append(
                f"{emoji} *{r['name']}* — {rain_pct}% · "
                f"{wx['max_temp']}°/{wx['min_temp']}°C · {wx['wind_kmh']} km/h"
            )
        chunk = []
        for line in lines:
            chunk.append(line)
            if len(chunk) == 8:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(chunk)}})
                chunk = []
        if chunk:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(chunk)}})

    if error_fields:
        blocks.append({"type": "divider"})
        names = ", ".join(r["name"] for r in error_fields)
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Fetch failed:* {names}"}}
        )

    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Open-Meteo · America/Toronto",
                }
            ],
        }
    )
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
    print("\nPosting…")
    post_to_slack(build_slack_blocks(results))


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


def cmd_server(args):
    if not SLACK_SIGNING_SECRET:
        print("Warning: SLACK_SIGNING_SECRET not set; requests are not verified.")
    Handler = make_handler()
    httpd = HTTPServer((args.host, args.port), Handler)
    print(f"Slash command URL: http://{args.host}:{args.port}/slack/command")
    print("Use HTTPS in Slack (e.g. ngrok http %s)" % args.port)
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
