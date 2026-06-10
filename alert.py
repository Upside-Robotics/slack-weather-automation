#!/usr/bin/env python3
"""
Sensor rain alert — polls modbus_sensor_readings every 10 min (via GitHub Actions cron).
Sends a Slack alert the first time rain is detected at a farm.
State (is_raining per farm) is persisted in the DB so repeat alerts are suppressed
until the rain stops and starts again.
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, "C:/Users/upsid/AppData/Roaming/Python/Python312/site-packages")
from dotenv import load_dotenv
import psycopg2

load_dotenv()

SLACK_WEBHOOK = "https://hooks.slack.com/services/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"

# How long before re-alerting a farm that is still raining (minutes)
ALERT_COOLDOWN_MINUTES = int(os.environ.get("ALERT_COOLDOWN_MINUTES", "60"))

# Only consider readings from the last N minutes to avoid acting on stale data
STALE_DATA_MINUTES = int(os.environ.get("STALE_DATA_MINUTES", "30"))


def db_connect():
    return psycopg2.connect(
        host=os.environ["POSTGRES_HOST"],
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        dbname=os.environ["POSTGRES_DATABASE"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
    )


def ensure_alert_log_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rain_alert_log (
                field_id        TEXT        PRIMARY KEY,
                farm_name       TEXT        NOT NULL,
                last_alert_at   TIMESTAMPTZ NOT NULL,
                is_raining      BOOLEAN     NOT NULL DEFAULT TRUE
            )
        """)
    conn.commit()


def fetch_latest_sensor_readings(conn):
    """
    Most recent modbus row per field (within STALE_DATA_MINUTES), joined to
    the fields table for the human-readable farm name.
    Returns list of dicts with: field_id, farm_name, soil_temp_c,
    soil_moisture_pct, rain_mm_per_query, event_time.
    """
    query = """
        SELECT DISTINCT ON (m.field_id)
            m.field_id,
            f.name                      AS farm_name,
            m.soil_sensor_temperature_c AS soil_temp_c,
            m.soil_sensor_humidity_pct  AS soil_moisture_pct,
            m.rain_sensor_mm_per_query  AS rain_mm_per_query,
            m.event_time
        FROM modbus_sensor_readings m
        LEFT JOIN fields f ON f.field_id = m.field_id
        WHERE m.event_time >= NOW() - INTERVAL '%s minutes'
          AND (f.is_test_field IS NULL OR f.is_test_field = FALSE)
        ORDER BY m.field_id, m.event_time DESC
    """
    with conn.cursor() as cur:
        cur.execute(query, (STALE_DATA_MINUTES,))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def fetch_ambient_temps(conn):
    """Most recent ambient temperature per field from the weather status table."""
    query = """
        SELECT DISTINCT ON (field_id)
            field_id,
            temperature AS ambient_temp_c
        FROM robot_pc_board_weather_status
        WHERE write_time >= NOW() - INTERVAL '30 minutes'
        ORDER BY field_id, write_time DESC
    """
    with conn.cursor() as cur:
        cur.execute(query)
        return {row[0]: row[1] for row in cur.fetchall()}


def get_alert_state(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT field_id, farm_name, last_alert_at, is_raining FROM rain_alert_log")
        return {
            row[0]: {"farm_name": row[1], "last_alert_at": row[2], "is_raining": row[3]}
            for row in cur.fetchall()
        }


def upsert_state(conn, field_id: str, farm_name: str, is_raining: bool, update_timestamp: bool):
    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        if update_timestamp:
            cur.execute("""
                INSERT INTO rain_alert_log (field_id, farm_name, last_alert_at, is_raining)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (field_id) DO UPDATE
                SET farm_name     = EXCLUDED.farm_name,
                    last_alert_at = EXCLUDED.last_alert_at,
                    is_raining    = EXCLUDED.is_raining
            """, (field_id, farm_name, now, is_raining))
        else:
            cur.execute("""
                INSERT INTO rain_alert_log (field_id, farm_name, last_alert_at, is_raining)
                VALUES (%s, %s, NOW(), %s)
                ON CONFLICT (field_id) DO UPDATE
                SET farm_name  = EXCLUDED.farm_name,
                    is_raining = EXCLUDED.is_raining
            """, (field_id, farm_name, is_raining))
    conn.commit()


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
    ensure_alert_log_table(conn)

    readings = fetch_latest_sensor_readings(conn)
    ambient = fetch_ambient_temps(conn)
    prior_state = get_alert_state(conn)
    now = datetime.now(timezone.utc)

    alerts: list[dict] = []

    for row in readings:
        field_id = str(row["field_id"])
        farm_name = row["farm_name"] or field_id

        try:
            rain_mm = float(row["rain_mm_per_query"]) if row["rain_mm_per_query"] is not None else 0.0
        except (TypeError, ValueError):
            rain_mm = 0.0

        is_raining = rain_mm > 0
        prior = prior_state.get(field_id, {})
        was_raining = prior.get("is_raining", False)
        last_alert = prior.get("last_alert_at")

        should_alert = False
        if is_raining:
            if not was_raining:
                should_alert = True  # rain just started
            elif last_alert:
                elapsed_min = (now - last_alert).total_seconds() / 60
                if elapsed_min >= ALERT_COOLDOWN_MINUTES:
                    should_alert = True  # still raining, re-alert after cooldown

        if should_alert:
            alerts.append({
                "field_id": field_id,
                "farm_name": farm_name,
                "rain_mm": rain_mm,
                "soil_temp_c": row.get("soil_temp_c"),
                "soil_moisture_pct": row.get("soil_moisture_pct"),
                "ambient_temp_c": ambient.get(field_id),
            })

        upsert_state(conn, field_id, farm_name, is_raining, update_timestamp=should_alert)

    conn.close()

    if alerts:
        lines = []
        for a in sorted(alerts, key=lambda x: x["farm_name"]):
            parts = [f"• *{a['farm_name']}* (`{a['field_id']}`) — Rain Detected"]
            details = []
            if a["soil_temp_c"] is not None:
                details.append(f"Soil temp: {a['soil_temp_c']:.1f}°C")
            if a["soil_moisture_pct"] is not None:
                details.append(f"Soil moisture: {a['soil_moisture_pct']:.1f}%")
            if a["ambient_temp_c"] is not None:
                details.append(f"Ambient: {a['ambient_temp_c']:.1f}°C")
            if details:
                parts.append("  " + " · ".join(details))
            lines.append("\n".join(parts))

        msg = ":rain_cloud: *Rain Detected*\n" + "\n".join(lines)
        post_slack(msg)
        print(f"Alert sent for {len(alerts)} farm(s): {', '.join(a['farm_name'] for a in alerts)}")
    else:
        print(f"No new rain events at {now:%Y-%m-%d %H:%M UTC}")


if __name__ == "__main__":
    main()
