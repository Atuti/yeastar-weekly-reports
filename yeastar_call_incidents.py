#!/usr/bin/env python3
"""Standalone Yeastar CDR incident drill-down; does not run or modify the weekly report."""
import argparse
import csv
import logging
import os
import sys
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("yeastar_call_incidents")
TZ = ZoneInfo(os.getenv("TIMEZONE", "Africa/Nairobi"))
BASE_URL = os.getenv("YEASTAR_BASE_URL", "").rstrip("/")
AUTH_VERSION = os.getenv("YEASTAR_AUTH_API_VERSION", "v1.0")
CDR_VERSION = os.getenv("YEASTAR_CDR_API_VERSION", "v2.0")
CLIENT_ID = os.getenv("YEASTAR_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("YEASTAR_CLIENT_SECRET", "")
VERIFY_TLS = os.getenv("YEASTAR_VERIFY_TLS", "true").lower() in ("1", "true", "yes")
PAGE_SIZE = min(max(int(os.getenv("YEASTAR_PAGE_SIZE", "10000")), 1), 10000)
PBX_TIME_FORMAT = os.getenv("YEASTAR_TIME_FORMAT", "%m/%d/%Y %H:%M:%S")

FIELDS = [
    "time", "call_type", "extension_match", "last_status", "outcome_group",
    "call_from_number", "call_from_name", "call_to_number", "call_to_name",
    "second_participant_number", "last_participant_number",
    "call_from", "call_to", "second_participant", "last_participant",
    "routing_duration", "handling_duration", "call_duration",
    "disconnected_by", "call_notes", "queues", "ring_groups", "ivrs",
    "call_flows", "segments", "source_trunks", "destination_trunks", "dids",
    "uid", "id", "raw_status",
]
GROUPS = {
    "ANSWERED": "Answered", "NO ANSWER": "No answer", "NOANSWER": "No answer",
    "BUSY": "Busy", "FAILED": "Failed", "ABANDONED": "Abandoned",
    "ABANDON": "Abandoned", "VOICEMAIL": "Voicemail",
}

def val(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        import json
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).strip()

def parse_time(value):
    for fmt in ("%Y/%m/%d %H:%M:%S", "%m/%d/%Y %H:%M:%S",
                "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %I:%M:%S %p",
                "%m/%d/%Y %I:%M:%S %p"):
        try:
            return datetime.strptime(str(value), fmt).replace(tzinfo=TZ)
        except (ValueError, TypeError):
            pass
    return None

def status_of(row):
    status = val(row.get("last_status")).upper()
    if status:
        return status
    candidate = val(row.get("disposition") or row.get("reason")).upper()
    aliases = {"ANSWER": "ANSWERED", "NOANSWER": "NO ANSWER",
               "ABANDON": "ABANDONED"}
    return aliases.get(candidate, candidate or "UNKNOWN")

def group_of(status):
    return GROUPS.get(status, "Other / unknown")

def default_period():
    today = datetime.now(TZ).date()
    monday = today - timedelta(days=today.weekday())
    return monday - timedelta(days=7), monday - timedelta(days=1)

class YeastarClient:
    def __init__(self):
        if not (BASE_URL and CLIENT_ID and CLIENT_SECRET):
            raise RuntimeError("Missing YEASTAR_BASE_URL, YEASTAR_CLIENT_ID, or YEASTAR_CLIENT_SECRET in .env")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "OpenAPI", "Content-Type": "application/json"})
        self.token = None

    def request(self, method, endpoint, *, params=None, body=None, auth=True, version=None):
        version = version or CDR_VERSION
        if auth and not self.token:
            self.login()
        params = dict(params or {})
        if auth:
            params["access_token"] = self.token

        def send():
            response = self.session.request(method, f"{BASE_URL}/openapi/{version}/{endpoint.lstrip('/')}",
                params=params, json=body, timeout=45, verify=VERIFY_TLS)
            response.raise_for_status()
            return response.json()

        payload = send()
        errcode = payload.get("errcode", 0)
        if errcode not in (0, "0", None):
            errmsg = str(payload.get("errmsg", ""))
            if auth and "token" in errmsg.lower():
                self.token = None
                self.login()
                params["access_token"] = self.token
                payload = send()
                errcode = payload.get("errcode", 0)
            if errcode not in (0, "0", None):
                raise RuntimeError(f"Yeastar API error {payload.get('errcode')}: {payload.get('errmsg')}")
        return payload

    def login(self):
        payload = self.request("POST", "get_token",
            body={"username": CLIENT_ID, "password": CLIENT_SECRET},
            auth=False, version=AUTH_VERSION)
        if not payload.get("access_token"):
            raise RuntimeError("Yeastar authentication response did not include access_token")
        self.token = payload["access_token"]
        LOG.info("Authenticated with Yeastar API (auth endpoint %s)", AUTH_VERSION)

    def get_cdrs(self, start, end):
        all_rows, page, total = [], 1, 0
        while True:
            payload = self.request("GET", "cdr/search", params={
                "page": page, "page_size": PAGE_SIZE, "order_by": "asc", "sort_by": "time",
                "time_begin": start.strftime(PBX_TIME_FORMAT),
                "time_end": end.strftime(PBX_TIME_FORMAT),
            }, version=CDR_VERSION)
            rows = payload.get("data") or []
            all_rows.extend(rows)
            total = int(payload.get("total_number", len(all_rows)) or 0)
            LOG.info("Fetched CDR page %s: %s rows (API total %s)", page, len(rows), total)
            if not rows or len(all_rows) >= total or len(rows) < PAGE_SIZE:
                break
            page += 1
        result = []
        for row in all_rows:
            dt = parse_time(row.get("time"))
            if dt and start <= dt <= end:
                item = dict(row)
                item["_dt"] = dt
                result.append(item)
        LOG.info("Fetched %s rows; %s fall inside requested window", len(all_rows), len(result))
        if all_rows and not result:
            raise RuntimeError("No returned CDR timestamps matched the selected window; check YEASTAR_TIME_FORMAT.")
        return result

def participants(row):
    keys = ("call_from_number", "call_to_number", "second_participant_number",
            "last_participant_number", "call_from", "call_to",
            "second_participant", "last_participant")
    return [val(row.get(k)) for k in keys if val(row.get(k))]

def matches_extension(row, extension):
    return not extension or any(x.casefold() == extension.casefold() for x in participants(row))

def unique_rows(rows):
    result = {}
    for row in rows:
        key = val(row.get("uid")) or val(row.get("id")) or "|".join(
            [val(row.get("time")), val(row.get("call_from_number")),
             val(row.get("call_to_number")), status_of(row)])
        score = sum(bool(val(row.get(k))) for k in FIELDS)
        old_score = sum(bool(val(result[key].get(k))) for k in FIELDS) if key in result else -1
        if score > old_score:
            result[key] = row
    return sorted(result.values(), key=lambda r: r.get("_dt"))

def csv_row(row, extension):
    status = status_of(row)
    out = {}
    for field in FIELDS:
        if field == "extension_match":
            out[field] = extension or "ALL"
        elif field == "outcome_group":
            out[field] = group_of(status)
        elif field == "raw_status":
            out[field] = val(row.get("last_status"))
        else:
            out[field] = val(row.get(field))
    return out

def main():
    default_start, default_end = default_period()
    parser = argparse.ArgumentParser(description="Inspect Yeastar call incidents separately from the weekly email report.")
    parser.add_argument("--start", default=default_start.isoformat(), help="Start date inclusive, YYYY-MM-DD; default previous Monday")
    parser.add_argument("--end", default=default_end.isoformat(), help="End date inclusive, YYYY-MM-DD; default previous Sunday")
    parser.add_argument("--extension", help="Filter by extension number, e.g. 1015")
    parser.add_argument("--all-statuses", action="store_true", help="Include answered calls; default only non-answered outcomes")
    parser.add_argument("--output", help="CSV output path")
    args = parser.parse_args()
    try:
        start_date, end_date = date.fromisoformat(args.start), date.fromisoformat(args.end)
    except ValueError as exc:
        parser.error(f"Dates must be YYYY-MM-DD: {exc}")
    if end_date < start_date:
        parser.error("--end must be on or after --start")
    start = datetime.combine(start_date, time.min, TZ)
    end = datetime.combine(end_date, time(23, 59, 59), TZ)

    print(f"Reporting period: {start} to {end} ({TZ.key})")
    print(f"PBX time format: {PBX_TIME_FORMAT}; CDR API: {CDR_VERSION}")
    print(f"Extension filter: {args.extension or 'ALL'}")
    rows = unique_rows(YeastarClient().get_cdrs(start, end))
    if args.extension:
        rows = [r for r in rows if matches_extension(r, args.extension)]
    if not args.all_statuses:
        rows = [r for r in rows if status_of(r) != "ANSWERED"]

    counts = Counter(status_of(r) for r in rows)
    print("\nOutcome summary")
    print("-" * 52)
    print(f"{'Outcome':<24} {'Count':>8} {'Share':>10}")
    for status, count in counts.most_common():
        print(f"{status:<24} {count:>8} {100 * count / len(rows) if rows else 0:>9.1f}%")
    print("-" * 52)
    print(f"{'TOTAL':<24} {len(rows):>8}")

    output = Path(args.output or f"yeastar_call_incidents_{start_date}_{end_date}.csv").expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(csv_row(r, args.extension) for r in rows)
    print(f"\nCSV written to: {output.resolve()}")
    print("\nMatching calls (latest 30)")
    print("-" * 110)
    print(f"{'TIME':<20} {'TYPE':<10} {'STATUS':<15} {'FROM':<18} {'TO':<18} UID")
    for r in rows[-30:]:
        print(f"{val(r.get('time')):<20.20} {val(r.get('call_type')):<10.10} "
              f"{status_of(r):<15.15} {val(r.get('call_from_number')):<18.18} "
              f"{val(r.get('call_to_number')):<18.18} {val(r.get('uid') or r.get('id'))}")
    if not rows:
        print("No matching calls. Check the date range and extension value.")
    print("\nNote: the sample CDR v2.0 schema has no dedicated failure-reason field. "
          "The CSV retains disconnected_by, call_notes, segments, durations, queues, "
          "ring groups, IVRs, call flows and trunk fields where supplied by the PBX. "
          "FAILED alone may not identify the underlying SIP/network cause.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
    except Exception:
        LOG.exception("Call incident drill-down failed")
        sys.exit(1)

