
import os
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from yeastar_weekly_report import YeastarClient, API_VERSION, TZ

load_dotenv()

client = YeastarClient()
client.login()

now = datetime.now(TZ)
monday = now.date() - timedelta(days=now.weekday())
start = datetime.combine(monday - timedelta(days=7), datetime.min.time(), TZ)
end = datetime.combine(monday - timedelta(days=1), datetime.max.time(), TZ)

time_format = os.getenv(
    "YEASTAR_TIME_FORMAT",
    "%Y/%m/%d %H:%M:%S",
)

print("API version:", API_VERSION)
print("Reporting period:", start, "to", end)
print("Configured PBX time format:", time_format)

# First query: do any CDRs exist without date filters?
raw = client._json_request(
    "GET",
    "cdr/search",
    params={"page": 1, "page_size": 5, "order_by": "desc", "sort_by": "time"},
)

print("\n--- UNFILTERED QUERY ---")
print("Total records reported:", raw.get("total_number"))
print("Response keys:", list(raw.keys()))

sample = raw.get("data") or []
print("Sample rows returned:", len(sample))

for row in sample[:3]:
    print(json.dumps({
        "keys": list(row.keys()),
        "time": row.get("time"),
        "call_type": row.get("call_type"),
        "last_status": row.get("last_status"),
        "uid": row.get("uid"),
    }, indent=2))

# Second query: same reporting dates as the weekly report.
filtered = client._json_request(
    "GET",
    "cdr/search",
    params={
        "page": 1,
        "page_size": 5,
        "order_by": "asc",
        "sort_by": "time",
        "time_begin": start.strftime(time_format),
        "time_end": end.strftime(time_format),
    },
)

print("\n--- DATE-FILTERED QUERY ---")
print("Requested start:", start.strftime(time_format))
print("Requested end:", end.strftime(time_format))
print("Total records reported:", filtered.get("total_number"))
print("Rows returned:", len(filtered.get("data") or []))

for row in (filtered.get("data") or [])[:3]:
    print(json.dumps({
        "time": row.get("time"),
        "call_type": row.get("call_type"),
        "last_status": row.get("last_status"),
    }, indent=2))

