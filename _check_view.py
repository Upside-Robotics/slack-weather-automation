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

# Latest hourly buckets from modbus_sensor_readings_1h
cur.execute("""
    SELECT DISTINCT ON (field_id)
        field_id,
        bucket_start,
        rain_total_mm_hour,
        soil_sensor_humidity_avg,
        soil_sensor_temperature_avg
    FROM modbus_sensor_readings_1h
    ORDER BY field_id, bucket_start DESC
    LIMIT 10
""")
cols = [d[0] for d in cur.description]
rows = cur.fetchall()
print("modbus_sensor_readings_1h — latest bucket per field (up to 10):")
for r in rows:
    print(dict(zip(cols, r)))

conn.close()
