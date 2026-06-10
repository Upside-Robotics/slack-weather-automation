import os, sys
sys.path.insert(0, "C:/Users/upsid/AppData/Roaming/Python/Python312/site-packages")
from dotenv import load_dotenv
load_dotenv()
import psycopg2

conn = psycopg2.connect(
    host=os.environ["POSTGRES_HOST"],
    port=os.environ["POSTGRES_PORT"],
    dbname=os.environ["POSTGRES_DATABASE"],
    user=os.environ["POSTGRES_USER"],
    password=os.environ["POSTGRES_PASSWORD"],
)
cur = conn.cursor()

views_to_check = [
    "base_pc_board_status_1h",
    "robot_pc_board_weather_status_1h",
    "modbus_sensor_readings_1h",
    "sht45_sensor_readings_1h",
]

for view in views_to_check:
    cur.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
        (view,)
    )
    rows = cur.fetchall()
    if rows:
        print(f"\n--- {view} ---")
        for col, dtype in rows:
            print(f"  {col}  ({dtype})")

# Also check actual tables
cur.execute(
    "SELECT table_name FROM information_schema.tables "
    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name"
)
print("\nTABLES:", [r[0] for r in cur.fetchall()])

conn.close()
