#!/usr/bin/env python3
"""
Sensor rain alert — polls modbus_sensor_readings every 10 min (via GitHub Actions cron).
Sends a Slack alert when a farm transitions from dry to raining, including a
10-minute reading history for context.

Detection:  raw modbus_sensor_readings — compares latest vs previous reading
            in a 30-min window (stateless, no DB writes required).
Context:    modbus_sensor_readings_1h view — hourly averages and rain total.
History:    last 10 min of raw readings per alerted farm, shown in the alert.
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

from dotenv import load_dotenv
import psycopg2

load_dotenv()

SLACK_WEBHOOK = os.environ.get("RAIN_ALERT_WEBHOOK_URL") or os.environ.get("SLACK_WEBHOOK_URL")

# base_station_id → {field_id, name}
BASE_STATION_FIELD_MAP = {
    "MRB241": {"field_id": "F1033", "name": "All 50"},
    "MRB213": {"field_id": "F1008", "name": "Brucelea Poultry"},
    "MRB222": {"field_id": "F1012", "name": "Burm 1 - East"},
    "MRB230": {"field_id": "F1013", "name": "Burm 2 - West"},
    "MRB228": {"field_id": "F1004", "name": "Clare Horst"},
    "MRB207": {"field_id": "F1002", "name": "Dougs-Cargill"},
    "MRB229": {"field_id": "F1001", "name": "Erin-Ed Home"},
    "MRB209": {"field_id": "F1019", "name": "Field and Flock 1 (Vienna)"},
    "MRB211": {"field_id": "F1020", "name": "Field and Flock 2 (Sparta)"},
    "MRB235": {"field_id": "F1011", "name": "Gerber Acres"},
    "MRB206": {"field_id": "F1017", "name": "Germar"},
    "MRB223": {"field_id": "F1037", "name": "Grant Con 6"},
    "MRB203": {"field_id": "F1014", "name": "Greg Leis"},
    "MRB219": {"field_id": "F1026", "name": "Harrison"},
    "MRB238": {"field_id": "F1022", "name": "Highland Farms"},
    "MRB234": {"field_id": "F1032", "name": "Home 69"},
    "MRB221": {"field_id": "F1029", "name": "Kerrigan"},
    "MRB245": {"field_id": "F1035", "name": "Klavan NorthWest"},
    "MRB246": {"field_id": "F1034", "name": "Klavan SouthEast"},
    "MRB204": {"field_id": "F1030", "name": "Lang"},
    "MRB220": {"field_id": "F1018", "name": "Martin Gerrits"},
    "MRB239": {"field_id": "F1021", "name": "Marvara"},
    "MRB243": {"field_id": "F1040", "name": "McAlpine"},
    "MRB216": {"field_id": "F1024", "name": "Moosberger"},
    "MRB242": {"field_id": "F1010", "name": "North-Earls"},
    "MRB208": {"field_id": "F1003", "name": "Peters 1"},
    "MRB236": {"field_id": "F1006", "name": "Renwick 1 (North)"},
    "MRB233": {"field_id": "F1007", "name": "Renwick 2 (South)"},
    "MRB231": {"field_id": "F1036", "name": "Research 74"},
    "MRB226": {"field_id": "F1000", "name": "Rod Roth (North 42+South 46)"},
    "MRB232": {"field_id": "F1016", "name": "Schaus / Brad Haack"},
    "MRB215": {"field_id": "F1015", "name": "Schumhaven"},
    "MRB217": {"field_id": "F1031", "name": "Scott Campbell"},
    "MRB225": {"field_id": "F1009", "name": "South-Earls"},
    "MRB227": {"field_id": "F1027", "name": "Sydenham 1"},
    "MRB224": {"field_id": "F1028", "name": "Sydenham 2 (Innis)"},
    "MRB240": {"field_id": "F1023", "name": "Triaro"},
    "MRB210": {"field_id": "F1005", "name": "Triple 1"},
    "MRB218": {"field_id": "F2",    "name": "Upside ODI"},
    "MRB214": {"field_id": "F1039", "name": "West 60"},
    "MRB237": {"field_id": "F1038", "name": "Wettlaufer"},
}

# Reverse: field_id → {base_station_id, name}
FIELD_MAP = {v["field_id"]: {"base_station": k, "name": v["name"]} for k, v in BASE_STATION_FIELD_MAP.items()}


def db_connect():
    return psycopg2.connect(
        host=os.environ["POSTGRES_HOST"],
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        dbname=os.environ["POSTGRES_DATABASE"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        connect_timeout=int(os.environ.get("POSTGRES_CONNECT_TIMEOUT", "10")),
    )


def fetch_readings(conn) -> list[dict]:
    """
    Latest and second-latest modbus reading per active field (30-min window).
    Transition: current_rain > 0 and prev_rain == 0 means rain just started.
    """
    query = """
        WITH ranked AS (
            SELECT
                m.field_id,
                f.name                       AS farm_name,
                m.soil_sensor_temperature_c,
                m.soil_sensor_humidity_pct,
                m.rain_sensor_mm_per_query,
                m.event_time,
                ROW_NUMBER() OVER (
                    PARTITION BY m.field_id
                    ORDER BY m.event_time DESC
                ) AS rn
            FROM modbus_sensor_readings m
            LEFT JOIN fields f ON f.field_id = m.field_id
            WHERE m.event_time >= NOW() - INTERVAL '60 minutes'
              AND (f.is_test_field IS NULL OR f.is_test_field = FALSE)
        )
        SELECT
            field_id,
            farm_name,
            MAX(CASE WHEN rn = 1 THEN rain_sensor_mm_per_query END)   AS current_rain,
            MAX(CASE WHEN rn = 2 THEN rain_sensor_mm_per_query END)   AS prev_rain,
            MAX(CASE WHEN rn = 1 THEN soil_sensor_temperature_c END)  AS soil_temp_c,
            MAX(CASE WHEN rn = 1 THEN soil_sensor_humidity_pct END)   AS soil_moisture_pct,
            MAX(CASE WHEN rn = 1 THEN event_time END)                 AS event_time
        FROM ranked
        GROUP BY field_id, farm_name
    """
    with conn.cursor() as cur:
        cur.execute(query)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def fetch_hourly_view(conn) -> dict[str, dict]:
    """Current-hour bucket from modbus_sensor_readings_1h for hourly context."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (field_id)
                field_id,
                rain_total_mm_hour,
                soil_sensor_temperature_avg,
                soil_sensor_humidity_avg
            FROM modbus_sensor_readings_1h
            ORDER BY field_id, bucket_start DESC
        """)
        return {
            row[0]: {
                "rain_total_mm_hour":    row[1],
                "soil_temp_avg_c":       row[2],
                "soil_moisture_avg_pct": row[3],
            }
            for row in cur.fetchall()
        }


def fetch_history(conn, field_ids: list[str]) -> dict[str, list[dict]]:
    """Last 10 minutes of raw readings per field, oldest first."""
    if not field_ids:
        return {}
    placeholders = ",".join(["%s"] * len(field_ids))
    query = f"""
        SELECT
            field_id,
            event_time,
            rain_sensor_mm_per_query,
            soil_sensor_temperature_c,
            soil_sensor_humidity_pct
        FROM modbus_sensor_readings
        WHERE field_id IN ({placeholders})
          AND event_time >= NOW() - INTERVAL '10 minutes'
        ORDER BY field_id, event_time ASC
    """
    with conn.cursor() as cur:
        cur.execute(query, field_ids)
        result: dict[str, list[dict]] = {fid: [] for fid in field_ids}
        for fid, ts, rain, soil_temp, soil_moist in cur.fetchall():
            result[fid].append({
                "ts":         ts,
                "rain":       rain,
                "soil_temp":  soil_temp,
                "soil_moist": soil_moist,
            })
    return result


def fetch_ambient_temps(conn) -> dict[str, float]:
    """Most recent ambient temperature per field (last 30 min)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (field_id)
                field_id,
                temperature
            FROM robot_pc_board_weather_status
            WHERE write_time >= NOW() - INTERVAL '30 minutes'
            ORDER BY field_id, write_time DESC
        """)
        return {row[0]: row[1] for row in cur.fetchall()}


def format_history(rows: list[dict]) -> str:
    if not rows:
        return "  _No readings in last 10 min_"
    lines = []
    for r in rows:
        ts = r["ts"].strftime("%H:%M:%S") if r["ts"] else "?"
        rain = f"{float(r['rain']):.2f}mm" if r["rain"] is not None else "—"
        temp = f"{float(r['soil_temp']):.1f}°C" if r["soil_temp"] is not None else "—"
        moist = f"{float(r['soil_moist']):.1f}%" if r["soil_moist"] is not None else "—"
        lines.append(f"  `{ts}` rain={rain}  soil={temp}  moist={moist}")
    return "\n".join(lines)


def post_slack(text: str):
    if not SLACK_WEBHOOK:
        print("SLACK_WEBHOOK_URL is not set; skipping Slack post", file=sys.stderr)
        return None

    payload = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        SLACK_WEBHOOK,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read()


def main():
    try:
        conn = db_connect()
    except psycopg2.OperationalError as exc:
        print(f"Database connection unavailable; skipping alert run: {exc}", file=sys.stderr)
        return

    readings = fetch_readings(conn)
    hourly = fetch_hourly_view(conn)
    ambient = fetch_ambient_temps(conn)

    alerts: list[dict] = []

    for row in readings:
        try:
            current_rain = float(row["current_rain"]) if row["current_rain"] is not None else 0.0
        except (TypeError, ValueError):
            current_rain = 0.0
        if current_rain > 0:
            fid = row["field_id"]
            h = hourly.get(fid, {})
            meta = FIELD_MAP.get(fid, {})
            alerts.append({
                "field_id":              fid,
                "farm_name":             meta.get("name") or row["farm_name"] or fid,
                "base_station":          meta.get("base_station", "—"),
                "rain_mm":               current_rain,
                "rain_total_mm_hour":    h.get("rain_total_mm_hour"),
                "soil_temp_avg_c":       h.get("soil_temp_avg_c"),
                "soil_moisture_avg_pct": h.get("soil_moisture_avg_pct"),
                "ambient_temp_c":        ambient.get(fid),
            })

    if alerts:
        alerted_fids = [a["field_id"] for a in alerts]
        history = fetch_history(conn, alerted_fids)

        lines = []
        for a in sorted(alerts, key=lambda x: x["farm_name"]):
            header = f"• *{a['farm_name']}* ({a['base_station']} · `{a['field_id']}`) — Rain Detected"
            details = []
            if a["rain_total_mm_hour"] is not None:
                details.append(f"This hour: {float(a['rain_total_mm_hour']):.1f} mm")
            if a["soil_temp_avg_c"] is not None:
                details.append(f"Soil temp: {float(a['soil_temp_avg_c']):.1f}°C")
            if a["soil_moisture_avg_pct"] is not None:
                details.append(f"Soil moisture: {float(a['soil_moisture_avg_pct']):.1f}%")
            if a["ambient_temp_c"] is not None:
                details.append(f"Ambient: {float(a['ambient_temp_c']):.1f}°C")

            hist_str = format_history(history.get(a["field_id"], []))
            detail_str = ("  " + " · ".join(details) + "\n") if details else ""
            lines.append(f"{header}\n{detail_str}  *Last 10 min:*\n{hist_str}")

        msg = ":rain_cloud: *Rain Detected*\n" + "\n\n".join(lines)
        post_slack(msg)
        print(f"Alert sent for {len(alerts)} farm(s): {', '.join(a['farm_name'] for a in alerts)}")
    else:
        print(f"No new rain events at {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}")

    conn.close()


if __name__ == "__main__":
    main()
