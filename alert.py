#!/usr/bin/env python3
"""
Sensor rain alert — polls modbus_sensor_readings every 10 min (via GitHub Actions cron).
Sends a Slack alert when a farm transitions from dry to raining.

Detection:  raw modbus_sensor_readings — compares latest vs previous reading
            in a 30-min window (stateless, no DB writes, timely).
Context:    modbus_sensor_readings_1h view — hourly averages and rain total
            included in the Slack message.
"""

import json
import os
import urllib.request
from datetime import datetime, timezone

from dotenv import load_dotenv
import psycopg2

load_dotenv()

SLACK_WEBHOOK = "https://hooks.slack.com/services/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"


def db_connect():
    return psycopg2.connect(
        host=os.environ["POSTGRES_HOST"],
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        dbname=os.environ["POSTGRES_DATABASE"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
    )


def fetch_readings(conn) -> list[dict]:
    """
    Returns one row per active field with:
      - current_rain: rain_sensor_mm_per_query from the most recent reading
      - prev_rain: same from the second-most-recent reading in the last 30 min
    If current_rain > 0 and prev_rain == 0 (or no previous), rain just started.
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
            WHERE m.event_time >= NOW() - INTERVAL '30 minutes'
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
    """
    Current-hour bucket from modbus_sensor_readings_1h.
    Provides hourly-averaged soil readings and total rain this hour.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (field_id)
                field_id,
                bucket_start,
                rain_total_mm_hour,
                soil_sensor_temperature_avg,
                soil_sensor_humidity_avg
            FROM modbus_sensor_readings_1h
            ORDER BY field_id, bucket_start DESC
        """)
        return {
            row[0]: {
                "bucket_start":          row[1],
                "rain_total_mm_hour":    row[2],
                "soil_temp_avg_c":       row[3],
                "soil_moisture_avg_pct": row[4],
            }
            for row in cur.fetchall()
        }


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


def post_slack(text: str):
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
    conn = db_connect()
    readings = fetch_readings(conn)
    hourly = fetch_hourly_view(conn)
    ambient = fetch_ambient_temps(conn)
    conn.close()

    alerts: list[dict] = []

    for row in readings:
        try:
            current_rain = float(row["current_rain"]) if row["current_rain"] is not None else 0.0
        except (TypeError, ValueError):
            current_rain = 0.0

        try:
            prev_rain = float(row["prev_rain"]) if row["prev_rain"] is not None else 0.0
        except (TypeError, ValueError):
            prev_rain = 0.0

        # Alert only on dry → raining transition
        if current_rain > 0 and prev_rain == 0:
            fid = row["field_id"]
            h = hourly.get(fid, {})
            alerts.append({
                "field_id":              fid,
                "farm_name":             row["farm_name"] or fid,
                "rain_mm":               current_rain,
                "rain_total_mm_hour":    h.get("rain_total_mm_hour"),
                "soil_temp_avg_c":       h.get("soil_temp_avg_c"),
                "soil_moisture_avg_pct": h.get("soil_moisture_avg_pct"),
                "ambient_temp_c":        ambient.get(fid),
            })

    now = datetime.now(timezone.utc)

    if alerts:
        lines = []
        for a in sorted(alerts, key=lambda x: x["farm_name"]):
            line = f"• *{a['farm_name']}* (`{a['field_id']}`) — Rain Detected"
            details = []
            if a["rain_total_mm_hour"] is not None:
                details.append(f"This hour: {float(a['rain_total_mm_hour']):.1f} mm")
            if a["soil_temp_avg_c"] is not None:
                details.append(f"Soil temp: {float(a['soil_temp_avg_c']):.1f}°C")
            if a["soil_moisture_avg_pct"] is not None:
                details.append(f"Soil moisture: {float(a['soil_moisture_avg_pct']):.1f}%")
            if a["ambient_temp_c"] is not None:
                details.append(f"Ambient: {float(a['ambient_temp_c']):.1f}°C")
            if details:
                line += "\n  " + " · ".join(details)
            lines.append(line)

        msg = ":rain_cloud: *Rain Detected*\n" + "\n".join(lines)
        post_slack(msg)
        print(f"Alert sent for {len(alerts)} farm(s): {', '.join(a['farm_name'] for a in alerts)}")
    else:
        print(f"No new rain events at {now:%Y-%m-%d %H:%M UTC}")


if __name__ == "__main__":
    main()
