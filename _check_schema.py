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

# All views
cur.execute("SELECT table_name FROM information_schema.views WHERE table_schema = 'public' ORDER BY table_name")
print("VIEWS:", [r[0] for r in cur.fetchall()])

# Check pc_board_status and base views if they exist
for view in ["base", "pc_board_status", "robot_pc_board_status"]:
    cur.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
        (view,)
    )
    rows = cur.fetchall()
    if rows:
        print(f"\n{view} columns:")
        for col, dtype in rows:
            print(f"  {col}  ({dtype})")

conn.close()
