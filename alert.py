#!/usr/bin/env python3
"""
Sensor rain alert — reads soil/ambient data from PostgreSQL.
Posts to Slack when rainfall is detected at a farm.
State is stored in the DB so the alert fires once per rain event
(not every 10-min cron tick). Re-alerts after ALERT_COOLDOWN_MINUTES
if rain is still ongoing.
"""

import json
import os
import urllib.request
from datetime import datetime, timezone

import psycopg2
from dotenv import load_dotenv

load_dotenv()

SLACK_WEBHOOK = "https://hooks.slack.com/services/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"

# Table and column names — override via .env if your schema differs
DB_TABLE          = os.environ.get("DB_TABLE", "sensor_readings")
COL_FARM          = os.environ.get("DB_COL_FARM", "farm_name")
COL_RAINFALL      = os.environ.get("DB_COL_RAINFALL", "rainfall")
COL_SOIL_TEMP     = os.environ.get("DB_COL_SOIL_TEMP", "soil_temperature")
COL_SOIL_MOISTURE = os.environ.get("DB_COL_SOIL_MOISTURE", "soil_moisture")
COL_AMBIENT_TEMP  = os.environ.get("DB_COL_AMBIENT_TEMP", "ambient_temperature")
COL_TIMESTAMP     = os.environ.get("DB_COL_TIMESTAMP", "recorded_at")

# How long before re-alerting for a farm that's still raining (minutes)
ALERT_COOLDOWN_MINUTES = int(os.environ.get("ALERT_COOLDOWN_MINUTES", "60"))


def db_connect():
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )


def ensure_alert_log_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rain_alert_log (
                farm_name       TEXT        PRIMARY KEY,
                last_alert_at   TIMESTAMPTZ NOT NULL,
                is_raining      BOOLEAN     NOT NULL DEFAULT TRUE
            )
        """)
    conn.commit()


def fetch_latest_readings(conn):
    """Most recent sensor row per farm."""
    query = f"""
        SELECT DISTINCT ON ({COL_FARM})
            {COL_FARM}          AS farm,
            {COL_SOIL_TEMP}     AS soil_temp,
            {COL_SOIL_MOISTURE} AS soil_moisture,
            {COL_AMBIENT_TEMP}  AS ambient_temp,
            {COL_RAINFALL}      AS rainfall,
            {COL_TIMESTAMP}     AS recorded_at
        FROM {DB_TABLE}
        ORDER BY {COL_FARM}, {COL_TIMESTAMP} DESC
    """
    with conn.cursor() as cur:
        cur.execute(query)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_alert_state(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT farm_name, last_alert_at, is_raining FROM rain_alert_log")
        return {
            row[0]: {"last_alert_at": row[1], "is_raining": row[2]}
            for row in cur.fetchall()
        }


def upsert_state(conn, farm: str, is_raining: bool, update_timestamp: bool):
    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        if update_timestamp:
            cur.execute("""
                INSERT INTO rain_alert_log (farm_name, last_alert_at, is_raining)
                VALUES (%s, %s, %s)
                ON CONFLICT (farm_name) DO UPDATE
                SET last_alert_at = EXCLUDED.last_alert_at,
                    is_raining    = EXCLUDED.is_raining
            """, (farm, now, is_raining))
        else:
            cur.execute("""
                INSERT INTO rain_alert_log (farm_name, last_alert_at, is_raining)
                VALUES (%s, NOW(), %s)
                ON CONFLICT (farm_name) DO UPDATE
                SET is_raining = EXCLUDED.is_raining
            """, (farm, is_raining))
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

    readings = fetch_latest_readings(conn)
    prior_state = get_alert_state(conn)
    now = datetime.now(timezone.utc)

    alerts: list[tuple[str, float]] = []

    for row in readings:
        farm = str(row["farm"])
        try:
            rainfall_mm = float(row["rainfall"]) if row["rainfall"] is not None else 0.0
        except (TypeError, ValueError):
            rainfall_mm = 0.0

        is_raining = rainfall_mm > 0
        prior = prior_state.get(farm, {})
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
            alerts.append((farm, rainfall_mm))

        upsert_state(conn, farm, is_raining, update_timestamp=should_alert)

    conn.close()

    if alerts:
        farm_lines = "\n".join(f"• {farm} — {mm:.1f} mm" for farm, mm in sorted(alerts))
        msg = f":rain_cloud: *Rain Detected*\n{farm_lines}"
        post_slack(msg)
        print(f"Alert sent for {len(alerts)} farm(s): {', '.join(f for f, _ in alerts)}")
    else:
        print(f"No new rain events at {now:%Y-%m-%d %H:%M UTC}")


if __name__ == "__main__":
    main()
