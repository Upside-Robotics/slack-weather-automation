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

for tbl in ["modbus_sensor_readings", "robot_pc_board_weather_status", "fields"]:
    cur.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
        (tbl,)
    )
    rows = cur.fetchall()
    print(f"\n--- {tbl} ---")
    for col, dtype in rows:
        print(f"  {col}  ({dtype})")

# Sample a few rows from modbus_sensor_readings
cur.execute("SELECT * FROM modbus_sensor_readings ORDER BY write_time DESC LIMIT 2")
cols = [d[0] for d in cur.description]
rows = cur.fetchall()
print("\n--- modbus_sensor_readings sample rows ---")
for row in rows:
    for c, v in zip(cols, row):
        print(f"  {c}: {v}")
    print()

conn.close()
