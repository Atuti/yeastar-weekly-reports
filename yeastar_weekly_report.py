#!/usr/bin/env python3
"""
Yeastar P-Series weekly departmental call report.
Designed for Yeastar P-Series Appliance Edition API (CDR v2.0).

Important:
- Map extensions, queues, and ring groups to departments in .env.
- CDR data supports call outcomes, durations, participants, queues, IVRs and ring groups.
- Callback/repeat-caller metrics are heuristics; validate against your call flows before using as formal KPIs.
"""
import os
import json
import logging
import smtplib
import statistics
from collections import Counter, defaultdict
from datetime import datetime, date, time, timedelta
from email.message import EmailMessage
from email.utils import formatdate
from html import escape
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
LOG = logging.getLogger("yeastar_report")

TZ = ZoneInfo(os.getenv("TIMEZONE", "Africa/Nairobi"))
BASE_URL = os.getenv("YEASTAR_BASE_URL", "").rstrip("/")
# Yeastar token authentication is v1.0. CDR v2.0 is used for detailed call outcomes.
AUTH_API_VERSION = os.getenv("YEASTAR_AUTH_API_VERSION", "v1.0")
CDR_API_VERSION = os.getenv("YEASTAR_CDR_API_VERSION", "v2.0")
# Backwards-compatible name for diagnostic scripts that import API_VERSION.
API_VERSION = CDR_API_VERSION
CLIENT_ID = os.getenv("YEASTAR_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("YEASTAR_CLIENT_SECRET", "")
VERIFY_TLS = os.getenv("YEASTAR_VERIFY_TLS", "true").lower() in ("1", "true", "yes")
PAGE_SIZE = min(int(os.getenv("YEASTAR_PAGE_SIZE", "10000")), 10000)

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() in ("1", "true", "yes")
EMAIL_TO = [x.strip() for x in os.getenv("EMAIL_TO", "").split(",") if x.strip()]
EMAIL_CC = [x.strip() for x in os.getenv("EMAIL_CC", "").split(",") if x.strip()]

# Example:
# {"Customer Experience":{"extensions":["1001","1002"],"queues":["Customer Service"],"ring_groups":["CX Ring Group"]}}
DEPARTMENTS = json.loads(os.getenv("DEPARTMENT_MAP_JSON", "{}"))
SLA = {
    "answer_seconds": int(os.getenv("SLA_ANSWER_SECONDS", "20")),
    "callback_hours": int(os.getenv("SLA_CALLBACK_HOURS", "4")),
    "escalation_hours": int(os.getenv("SLA_ESCALATION_HOURS", "4")),
}

def require_config():
    missing = []
    for key, val in {
        "YEASTAR_BASE_URL": BASE_URL,
        "YEASTAR_CLIENT_ID": CLIENT_ID,
        "YEASTAR_CLIENT_SECRET": CLIENT_SECRET,
        "SMTP_HOST": SMTP_HOST,
        "SMTP_FROM": SMTP_FROM,
        "EMAIL_TO": EMAIL_TO,
        "DEPARTMENT_MAP_JSON": DEPARTMENTS,
    }.items():
        if not val:
            missing.append(key)
    if missing:
        raise RuntimeError("Missing configuration: " + ", ".join(missing))
    if not DEPARTMENTS:
        raise RuntimeError("DEPARTMENT_MAP_JSON must map your departments to extensions/queues/ring groups.")

class YeastarClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "OpenAPI", "Content-Type": "application/json"})
        self.token = None

    def _url(self, endpoint, api_version=None):
        version = api_version or CDR_API_VERSION
        return f"{BASE_URL}/openapi/{version}/{endpoint.lstrip('/')}"

    def _json_request(
        self, method, endpoint, *, params=None, body=None, auth=True, api_version=None
    ):
        if auth and not self.token:
            self.login()
        request_params = dict(params or {})
        if auth:
            request_params["access_token"] = self.token

        def send():
            response = self.session.request(
                method,
                self._url(endpoint, api_version=api_version),
                params=request_params,
                json=body,
                timeout=45,
                verify=VERIFY_TLS,
            )
            response.raise_for_status()
            return response.json()

        payload = send()
        errcode = payload.get("errcode", 0)
        if errcode not in (0, "0", None):
            errmsg = str(payload.get("errmsg", ""))
            # Re-authenticate once if the access token has expired.
            if auth and "token" in errmsg.lower():
                self.token = None
                self.login()
                request_params["access_token"] = self.token
                payload = send()
                errcode = payload.get("errcode", 0)
            if errcode not in (0, "0", None):
                raise RuntimeError(
                    f"Yeastar API error {payload.get('errcode')}: {payload.get('errmsg')}"
                )
        return payload

    def login(self):
        payload = self._json_request(
            "POST",
            "get_token",
            body={"username": CLIENT_ID, "password": CLIENT_SECRET},
            auth=False,
            api_version=AUTH_API_VERSION,
        )
        if not payload.get("access_token"):
            raise RuntimeError("Yeastar authentication response did not include access_token")
        self.token = payload["access_token"]
        LOG.info("Authenticated with Yeastar API (auth endpoint %s)", AUTH_API_VERSION)

    def get_cdrs(self, start_dt, end_dt):
        # CDR v2.0 uses time_begin/time_end. Format MUST match the PBX display format.
        # The user's PBX sample was MM/DD/YYYY HH:mm:ss (e.g. 07/23/2026 12:50:29).
        time_format = os.getenv("YEASTAR_TIME_FORMAT", "%m/%d/%Y %H:%M:%S")
        all_rows, page = [], 1
        while True:
            payload = self._json_request(
                "GET", "cdr/search",
                params={
                    "page": page,
                    "page_size": PAGE_SIZE,
                    "order_by": "asc",
                    "sort_by": "time",
                    "time_begin": start_dt.strftime(time_format),
                    "time_end": end_dt.strftime(time_format),
                },
                api_version=CDR_API_VERSION,
            )
            rows = payload.get("data") or []
            all_rows.extend(rows)
            total = int(payload.get("total_number", len(all_rows)))
            LOG.info("Fetched CDR page %s: %s rows (total reported %s)", page, len(rows), total)
            if not rows or len(all_rows) >= total or len(rows) < PAGE_SIZE:
                break
            page += 1
        # Fail closed if the PBX did not honor the date filters.
        parsed_in_window = [
            row for row in all_rows
            if (parse_time(row.get("time")) is not None
                and start_dt <= parse_time(row.get("time")) <= end_dt)
        ]
        LOG.info(
            "CDR endpoint=%s; rows fetched=%s; rows in requested window=%s; API total_number=%s",
            CDR_API_VERSION, len(all_rows), len(parsed_in_window), total if all_rows else 0
        )
        if all_rows and not parsed_in_window:
            sample_times = [strval(row.get("time")) for row in all_rows[:3]]
            raise RuntimeError(
                "Yeastar returned CDRs, but none are within the requested reporting window. "
                "Check CDR API version and YEASTAR_TIME_FORMAT. Sample PBX times: "
                + repr(sample_times)
            )
        return parsed_in_window

def parse_time(value):
    if not value:
        return None
    for fmt in (
        "%Y/%m/%d %H:%M:%S", "%m/%d/%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %I:%M:%S %p",
        "%m/%d/%Y %I:%M:%S %p",
    ):
        try:
            parsed = datetime.strptime(str(value), fmt)
            return parsed.replace(tzinfo=TZ)
        except ValueError:
            pass
    return None

def strval(value):
    return "" if value is None else str(value).strip()

def as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]

def digits(value):
    return "".join(ch for ch in strval(value) if ch.isdigit())

def get_team_for_call(row):
    """Returns department using queue/ring-group names/numbers first, then extension mapping."""
    queue_values = []
    for q in as_list(row.get("queues")):
        if isinstance(q, dict):
            queue_values += [strval(q.get("name")), strval(q.get("number"))]
    rg_values = []
    for rg in as_list(row.get("ring_groups")):
        if isinstance(rg, dict):
            rg_values += [strval(rg.get("name")), strval(rg.get("number"))]
    from_num = strval(row.get("call_from_number"))
    to_num = strval(row.get("call_to_number"))
    second_num = strval(row.get("second_participant_number"))
    last_num = strval(row.get("last_participant_number"))
    participants = {from_num, to_num, second_num, last_num}
    for dept, cfg in DEPARTMENTS.items():
        queues = {str(x).casefold() for x in cfg.get("queues", [])}
        rings = {str(x).casefold() for x in cfg.get("ring_groups", [])}
        extensions = {str(x) for x in cfg.get("extensions", [])}
        if queues.intersection(x.casefold() for x in queue_values if x):
            return dept
        if rings.intersection(x.casefold() for x in rg_values if x):
            return dept
        if extensions.intersection(participants):
            return dept
    return "Unmapped"

def agent_for_call(row):
    # Prefer the last participant on routed calls; otherwise use the relevant extension endpoint.
    for field in ("last_participant_number", "second_participant_number", "call_to_number", "call_from_number"):
        value = strval(row.get(field))
        if value and any(value in {str(x) for x in cfg.get("extensions", [])} for cfg in DEPARTMENTS.values()):
            return value
    return "Unknown"

def call_status(row):
    # CDR v2.0 uses last_status. Keep a conservative fallback for installations
    # whose response has legacy disposition/reason fields instead.
    status = strval(row.get("last_status")).upper()
    if status:
        return status
    legacy = strval(row.get("disposition")).upper()
    reason = strval(row.get("reason")).upper()
    candidate = legacy or reason
    aliases = {
        "ANSWER": "ANSWERED", "ANSWERED": "ANSWERED", "NO ANSWER": "NO ANSWER",
        "NOANSWER": "NO ANSWER", "ABANDON": "ABANDONED", "ABANDONED": "ABANDONED",
        "BUSY": "BUSY", "FAILED": "FAILED", "VOICEMAIL": "VOICEMAIL",
    }
    return aliases.get(candidate, "UNKNOWN")

def is_inbound(row):
    return strval(row.get("call_type")).lower() == "inbound"

def is_outbound(row):
    return strval(row.get("call_type")).lower() == "outbound"

def is_answered(row):
    return call_status(row) == "ANSWERED"

def is_missed(row):
    return call_status(row) in {"NO ANSWER", "ABANDONED", "BUSY", "FAILED"}

def fmt_seconds(seconds):
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 60}m {seconds % 60:02d}s"

def pct(n, total):
    return (100.0 * n / total) if total else 0.0

def table(headers, rows):
    th = "".join(f"<th style='text-align:left;padding:8px;background:#222;color:#fff'>{escape(str(h))}</th>" for h in headers)
    trs = []
    for row in rows:
        cells = "".join(f"<td style='padding:7px;border-bottom:1px solid #ddd'>{escape(str(c))}</td>" for c in row)
        trs.append(f"<tr>{cells}</tr>")
    return f"<table style='border-collapse:collapse;width:100%;font-size:13px;margin:8px 0 18px'><thead><tr>{th}</tr></thead><tbody>{''.join(trs)}</tbody></table>"

def analyze(rows, period_start, period_end):
    # Keep rows inside the reporting period; API returns timestamps in PBX display time.
    dated = []
    for r in rows:
        dt = parse_time(r.get("time"))
        if dt and period_start <= dt <= period_end:
            item = dict(r)
            item["_dt"] = dt
            item["_dept"] = get_team_for_call(r)
            item["_agent"] = agent_for_call(r)
            item["_status"] = call_status(r)
            dated.append(item)

    # CDRs may contain multiple legs; report distinct UID where possible to reduce obvious duplicates.
    unique = {}
    for r in dated:
        key = strval(r.get("uid")) or strval(r.get("id")) or f"{r['_dt']}|{r.get('call_from_number')}|{r.get('call_to_number')}"
        # Preserve a record with the most segments/details for that UID.
        if key not in unique or int(r.get("segments") or 0) > int(unique[key].get("segments") or 0):
            unique[key] = r
    calls = list(unique.values())

    departments = sorted(set(DEPARTMENTS) | {r["_dept"] for r in calls})
    dept_data = {}
    for dept in departments:
        cr = [r for r in calls if r["_dept"] == dept]
        inbound = [r for r in cr if is_inbound(r)]
        outbound = [r for r in cr if is_outbound(r)]
        answered = [r for r in inbound if is_answered(r)]
        missed = [r for r in inbound if is_missed(r)]
        voicemail = [r for r in inbound if r["_status"] == "VOICEMAIL"]
        abandoned = [r for r in inbound if r["_status"] == "ABANDONED"]
        no_answer = [r for r in inbound if r["_status"] == "NO ANSWER"]
        answer_times = [int(r.get("routing_duration") or 0) for r in answered]
        handling = [int(r.get("handling_duration") or 0) for r in answered]
        dept_data[dept] = {
            "all": cr, "inbound": inbound, "outbound": outbound,
            "answered": answered, "missed": missed, "voicemail": voicemail,
            "abandoned": abandoned, "no_answer": no_answer,
            "avg_answer": round(statistics.mean(answer_times), 1) if answer_times else 0,
            "avg_handling": round(statistics.mean(handling), 1) if handling else 0,
            "outbound_connected": [r for r in outbound if is_answered(r)],
        }

    # Callback heuristic: same external caller number is called back by an extension
    # after an unanswered/voicemail inbound call, within the visible reporting period.
    outbound = [r for r in calls if is_outbound(r) and is_answered(r)]
    callback_results = []
    for missed_call in [r for r in calls if is_inbound(r) and (is_missed(r) or r["_status"] == "VOICEMAIL")]:
        caller = digits(missed_call.get("call_from_number"))
        if len(caller) < 6:
            continue
        candidates = []
        for out in outbound:
            called_number = digits(out.get("call_to_number"))
            if called_number and (called_number.endswith(caller[-9:]) or caller.endswith(called_number[-9:])):
                delta = (out["_dt"] - missed_call["_dt"]).total_seconds()
                if 0 <= delta <= SLA["callback_hours"] * 3600:
                    candidates.append((delta, out))
        callback_results.append((missed_call, min(candidates, key=lambda x: x[0])[0] if candidates else None))

    # Repeat caller count: callers with >1 inbound call in the reporting period.
    caller_counts = Counter(digits(r.get("call_from_number")) for r in calls if is_inbound(r) and len(digits(r.get("call_from_number"))) >= 6)
    repeats = {num: count for num, count in caller_counts.items() if count > 1}

    by_hour = Counter(r["_dt"].strftime("%A %H:00") for r in calls if is_inbound(r) and (is_missed(r) or r["_status"] == "VOICEMAIL"))
    by_day = Counter(r["_dt"].strftime("%A") for r in calls if is_inbound(r) and (is_missed(r) or r["_status"] == "VOICEMAIL"))
    agent_stats = defaultdict(list)
    for r in calls:
        if r["_dept"] != "Unmapped":
            agent_stats[(r["_dept"], r["_agent"])].append(r)

    return {
        "calls": calls, "dept_data": dept_data, "callback_results": callback_results,
        "repeats": repeats, "by_hour": by_hour, "by_day": by_day, "agent_stats": agent_stats,
    }

def build_html(result, start, end):
    calls = result["calls"]
    ddata = result["dept_data"]
    total_in = [r for r in calls if is_inbound(r)]
    total_out = [r for r in calls if is_outbound(r)]
    total_answered = [r for r in total_in if is_answered(r)]
    total_missed = [r for r in total_in if is_missed(r)]
    total_vm = [r for r in total_in if r["_status"] == "VOICEMAIL"]
    total_abandoned = [r for r in total_in if r["_status"] == "ABANDONED"]
    total_out_connected = [r for r in total_out if is_answered(r)]

    summary_rows = []
    for dept, d in ddata.items():
        n = len(d["inbound"])
        summary_rows.append([
            dept, n, f"{len(d['answered'])} ({pct(len(d['answered']), n):.1f}%)",
            f"{len(d['no_answer'])} ({pct(len(d['no_answer']), n):.1f}%)",
            f"{len(d['abandoned'])} ({pct(len(d['abandoned']), n):.1f}%)",
            f"{len(d['voicemail'])} ({pct(len(d['voicemail']), n):.1f}%)",
            len(d["outbound"]), f"{len(d['outbound_connected'])} ({pct(len(d['outbound_connected']), len(d['outbound'])):.1f}%)",
            fmt_seconds(d["avg_answer"]), fmt_seconds(d["avg_handling"]),
        ])

    department_sections = []
    for dept, d in ddata.items():
        agent_rows = []
        for (agent_dept, agent), agent_calls in sorted(result["agent_stats"].items()):
            if agent_dept != dept:
                continue
            answered = [r for r in agent_calls if is_answered(r)]
            missed = [r for r in agent_calls if is_missed(r)]
            durations = [int(r.get("handling_duration") or 0) for r in answered]
            avg_handle = round(statistics.mean(durations), 1) if durations else 0
            agent_rows.append([agent, len(agent_calls), len(answered), len(missed), fmt_seconds(avg_handle)])
        if not agent_rows:
            agent_rows = [["No mapped agent records", "-", "-", "-", "-"]]

        queue_counts = Counter()
        ivr_counts = Counter()
        transfer_counts = Counter()
        for r in d["inbound"]:
            for q in as_list(r.get("queues")):
                if isinstance(q, dict):
                    queue_counts[f"{q.get('name', '')} ({q.get('status', '')})"] += 1
            for ivr in as_list(r.get("ivrs")):
                if isinstance(ivr, dict):
                    ivr_counts[strval(ivr.get("name") or ivr.get("number") or ivr)] += 1
                elif ivr:
                    ivr_counts[strval(ivr)] += 1
            # Approximate transfer/routing indicators; not all transfers can be inferred from a CDR.
            if int(r.get("segments") or 1) > 1:
                transfer_counts[strval(r.get("last_participant_number") or r.get("last_participant") or "Multiple call legs")] += 1

        peak_day = result["by_day"].most_common(1)
        peak_hour = result["by_hour"].most_common(5)
        cb = [(m, delay) for m, delay in result["callback_results"] if m["_dept"] == dept]
        cb_returned = [x for x in cb if x[1] is not None]
        callback_rows = [
            ["Missed/voicemail calls", len(cb)],
            ["Likely callbacks within configured window", len(cb_returned)],
            ["No matching callback found", len(cb) - len(cb_returned)],
            ["Callback match rate", f"{pct(len(cb_returned), len(cb)):.1f}%"],
            ["Average matched callback delay", fmt_seconds(statistics.mean([x[1] for x in cb_returned])) if cb_returned else "N/A"],
        ]
        dept_peak_rows = [[day, n] for day, n in result["by_day"].most_common()]
        hour_rows = [[hour, n] for hour, n in peak_hour] or [["No missed/voicemail calls", 0]]
        queue_rows = [[name, count] for name, count in queue_counts.most_common(10)] or [["No queue details in CDR", 0]]
        ivr_rows = [[name, count] for name, count in ivr_counts.most_common(10)] or [["No IVR details in CDR", 0]]
        transfer_rows = [[dest, count] for dest, count in transfer_counts.most_common(10)] or [["No multi-leg calls identified", 0]]

        department_sections.append(f"""
        <h2>{escape(dept)}</h2>
        <p><b>Inbound:</b> {len(d['inbound'])}; <b>answered:</b> {len(d['answered'])} ({pct(len(d['answered']),len(d['inbound'])):.1f}%);
        <b>missed/failed/busy:</b> {len(d['missed'])} ({pct(len(d['missed']),len(d['inbound'])):.1f}%);
        <b>abandoned:</b> {len(d['abandoned'])}; <b>voicemail:</b> {len(d['voicemail'])}.
        <b>Outbound:</b> {len(d['outbound'])}; connected {len(d['outbound_connected'])} ({pct(len(d['outbound_connected']),len(d['outbound'])):.1f}%).</p>
        <h3>Agent / extension performance</h3>{table(["Agent/extension","CDRs","Answered","Missed/failed","Avg handling"], agent_rows)}
        <h3>Callback analysis (heuristic)</h3>{table(["Metric","Value"], callback_rows)}
        <h3>Queue / ring-group status entries</h3>{table(["Queue or status","CDR entries"], queue_rows)}
        <h3>IVR references (not a full menu drop-off funnel)</h3>{table(["IVR reference","CDR entries"], ivr_rows)}
        <h3>Multi-leg calls (possible transfer/routing indicators)</h3>{table(["Last participant / destination","Count"], transfer_rows)}
        """)

    day_rows = [[day, n] for day, n in result["by_day"].most_common()] or [["None", 0]]
    hour_rows = [[hour, n] for hour, n in result["by_hour"].most_common(10)] or [["None", 0]]
    repeat_rows = [[num, count] for num, count in sorted(result["repeats"].items(), key=lambda x: -x[1])[:20]] or [["None", 0]]

    return f"""<!doctype html><html><body style="font-family:Arial,sans-serif;color:#222;max-width:1100px;margin:auto">
    <div style="background:#171717;color:#fff;padding:20px;border-bottom:5px solid #c9a227">
      <h1 style="margin:0">Yeastar Weekly Call Performance</h1>
      <p style="margin-bottom:0">Reporting period: {start:%d %b %Y} – {end:%d %b %Y}</p>
    </div>
    <p><b>Executive summary.</b> This report is generated from PBX CDR records and department mapping configured by IT.</p>
    {table(["Measure","Count","Share of inbound"], [
        ["Inbound calls", len(total_in), "100.0%"],
        ["Answered", len(total_answered), f"{pct(len(total_answered),len(total_in)):.1f}%"],
        ["No answer / busy / failed", len(total_missed), f"{pct(len(total_missed),len(total_in)):.1f}%"],
        ["Abandoned", len(total_abandoned), f"{pct(len(total_abandoned),len(total_in)):.1f}%"],
        ["Voicemail", len(total_vm), f"{pct(len(total_vm),len(total_in)):.1f}%"],
        ["Outbound calls", len(total_out), "—"],
        ["Outbound connected", len(total_out_connected), f"{pct(len(total_out_connected),len(total_out)):.1f}%"],
        ["Repeat external caller numbers", len(result["repeats"]), "—"],
    ])}
    <h2>Department comparison</h2>
    {table(["Department","Inbound","Answered","No answer","Abandoned","Voicemail","Outbound","Outbound connected","Avg answer delay","Avg handling"], summary_rows)}
    <h2>When unanswered / voicemail calls occur</h2>
    <h3>By day</h3>{table(["Day","Count"], day_rows)}
    <h3>Top day-and-hour windows</h3>{table(["Day / hour","Missed or voicemail"], hour_rows)}
    <h2>Repeat callers</h2>
    <p>Numbers below called more than once during the reporting period. Handle phone numbers as confidential personal data.</p>
    {table(["Caller number","Inbound calls"], repeat_rows)}
    {''.join(department_sections)}
    <h2>Interpretation and data limitations</h2>
    <ul>
      <li>“Missed/failed” combines NO ANSWER, BUSY and FAILED. “Abandoned” and “VOICEMAIL” are separate outcomes.</li>
      <li>Average answer delay uses routing_duration for answered calls. Average handling uses handling_duration for answered calls.</li>
      <li>Callback matching is a best-effort match of the same caller number to an answered outbound CDR within {SLA['callback_hours']} hours. A missing match does not prove no callback occurred; different caller-ID formats, other channels or a callback outside the window can affect it.</li>
    </ul>
    <p style="font-size:11px;color:#666">Generated automatically by the Yeastar reporting service.</p>
    </body></html>"""

def send_email(subject, html):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = ", ".join(EMAIL_TO)
    if EMAIL_CC:
        msg["Cc"] = ", ".join(EMAIL_CC)
    msg["Date"] = formatdate(localtime=True)
    msg.set_content("This email contains an HTML weekly Yeastar call performance report. Please view it in an HTML-capable email client.")
    msg.add_alternative(html, subtype="html")
    recipients = EMAIL_TO + EMAIL_CC
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.ehlo()
        if SMTP_USE_TLS:
            server.starttls()
            server.ehlo()
        if SMTP_USER:
            server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg, to_addrs=recipients)
    LOG.info("Report email sent to %s recipient(s)", len(recipients))

def main():
    require_config()
    now = datetime.now(TZ)
    # Previous complete Monday-Sunday week, using local time.
    this_monday = (now.date() - timedelta(days=now.weekday()))
    previous_monday = this_monday - timedelta(days=7)
    previous_sunday = this_monday - timedelta(days=1)
    start = datetime.combine(previous_monday, time.min, TZ)
    end = datetime.combine(previous_sunday, time(23, 59, 59), TZ)

    client = YeastarClient()
    # Pull only the report period; expand to 14 days if you want richer callback matching.
    rows = client.get_cdrs(start, end)
    LOG.info("Fetched %s CDR records for reporting window", len(rows))
    if not rows:
        raise RuntimeError(
            "No CDR records were returned for the reporting period. "
            "Email was not sent to avoid a misleading zero-activity report."
        )
    result = analyze(rows, start, end)
    if not result["calls"]:
        raise RuntimeError(
            "CDR records were returned, but none could be parsed into the reporting period. "
            "Email was not sent. Check YEASTAR_TIME_FORMAT against the PBX display format."
        )
    LOG.info("Analyzed %s unique CDR records for the reporting period", len(result["calls"]))
    html = build_html(result, start, end)
    subject = f"Yeastar Weekly Call Performance | {start:%d %b}–{end:%d %b %Y}"
    send_email(subject, html)

if __name__ == "__main__":
    try:
        main()
    except Exception:
        LOG.exception("Weekly Yeastar report failed")
        raise

