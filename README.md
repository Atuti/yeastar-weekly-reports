# Yeastar P-560 Weekly Departmental Call Reports

## What it does
- Authenticates to the Yeastar P-Series API.
- Retrieves CDR v2.0 records for the previous complete Monday-Sunday period.
- Produces one consolidated HTML email with department, outcome, hourly/day patterns, outbound, agent/extension, repeat-caller, queue/IVR-reference and heuristic callback sections.
- Uses the PBX's `routing_duration`, `handling_duration`, `last_status`, `queues`, `ivrs`, `ring_groups`, `segments` and participant fields where available.

## Important limitations
This is a practical first release, not a complete contact-centre analytics platform. CDR alone may not provide complete queue-offer history, every transfer target, IVR keypress drop-off, escalation reason/resolution, or a definitive callback relationship. Callback matching is a heuristic and should not be treated as proof of agent compliance. Validate totals against the PBX UI and downloadable CDR before distributing to management.

## 1. Install on the VPS
```bash
sudo apt update
sudo apt install -y python3 python3-venv
sudo mkdir -p /opt/yeastar-report
sudo chown "$USER":"$USER" /opt/yeastar-report
cd /opt/yeastar-report
# Copy yeastar_weekly_report.py and .env here.
python3 -m venv .venv
. .venv/bin/activate
pip install requests python-dotenv
chmod 600 .env
```

## 2. Configure `.env`
Copy `.env.example` to `.env`, then set:
- `YEASTAR_BASE_URL`: reachable PBX HTTPS URL.
- Client ID/Secret from the Yeastar API settings.
- `YEASTAR_TIME_FORMAT`: must match PBX display format.
- SMTP details.
- `DEPARTMENT_MAP_JSON`: replace all example extensions, queue names and ring-group names.

Do not disable TLS verification in production. If the VPS cannot reach the PBX directly, configure and restrict secure remote API access or a VPN; do not expose the PBX API broadly to the internet.

## 3. Test manually
```bash
cd /opt/yeastar-report
.venv/bin/python yeastar_weekly_report.py
```
The script emails the previous complete Monday-Sunday week. Test with a small recipient list first and compare its totals with the PBX UI/CDR export.

## 4. Schedule every Sunday
Edit cron:
```bash
crontab -e
```
Add the following to run Sunday at 07:00 Nairobi time:
```cron
0 7 * * 0 cd /opt/yeastar-report && /opt/yeastar-report/.venv/bin/python /opt/yeastar-report/yeastar_weekly_report.py >> /opt/yeastar-report/report.log 2>&1
```
Ensure the VPS timezone is Africa/Nairobi (`timedatectl`) or use a systemd timer configured for the intended timezone. On Ubuntu cron typically uses the system timezone.

## Suggested rollout
1. Week 1: send only to IT and validate against CDR exports.
2. Week 2: fix extension/queue mapping and confirm department attribution.
3. Then distribute to management and department heads.
4. Agree formal service targets with each department; the sample 20-second answer and 4-hour callback/escalation thresholds are placeholders, not official policies.

## Security
- Keep `.env` readable only by the service account.
- Use a dedicated Yeastar API client with minimum required permissions.
- Restrict VPS-to-PBX network access to the VPS IP/VPN.
- Call data includes personal phone numbers; limit recipients and retention.

