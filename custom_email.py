#!/usr/bin/env python3
"""
Bulk email sender using SMTP and a CSV recipient list.

CSV format (header row required):
    email,name,company
    alice@example.com,Alice,Acme
    bob@example.com,Bob,Globex

The message body and subject can include {placeholders} that match CSV column names.
"""

import csv
import smtplib
import ssl
import time
import logging
from email.message import EmailMessage
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SMTP_HOST = "smtp.office365.com"
SMTP_PORT = 587                  # 587 = STARTTLS, 465 = SSL
SMTP_USER = "info@we-ace.com"
SMTP_PASSWORD = "d-Wurij16376Clou@#$"   # Use an app password, not your login password
USE_SSL = False                  # True for port 465, False for 587 STARTTLS

FROM_ADDRESS = "info@we-ace.com"
FROM_NAME = "Info Weace"

CSV_FILE = "recipients.csv"
EMAIL_COLUMN = "email"           # which CSV column holds the address

SUBJECT_TEMPLATE = "Hi {name}, a quick note for {company}"
BODY_TEMPLATE = """\
Hi {name},

I'm reaching out because I thought this might be relevant to your work at {company}.

[Your message here.]

Best,
{from_name}
"""

# Optional HTML version (set to None to send plain text only)
HTML_TEMPLATE = """\
<p>Hi {name},</p>
<p>I'm reaching out because I thought this might be relevant to your work at <strong>{company}</strong>.</p>
<p>[Your message here.]</p>
<p>Best,<br>{from_name}</p>
"""

THROTTLE_SECONDS = 1.0           # delay between sends to avoid rate limits
LOG_FILE = "send_log.csv"        # records success/failure per recipient

# ---------------------------------------------------------------------------
# Logic
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("mailer")


def build_message(row: dict) -> EmailMessage:
    """Build an EmailMessage from a CSV row using the configured templates."""
    fields = {**row, "from_name": FROM_NAME}

    msg = EmailMessage()
    msg["From"] = f"{FROM_NAME} <{FROM_ADDRESS}>"
    msg["To"] = row[EMAIL_COLUMN]
    msg["Subject"] = SUBJECT_TEMPLATE.format(**fields)
    msg.set_content(BODY_TEMPLATE.format(**fields))

    if HTML_TEMPLATE:
        msg.add_alternative(HTML_TEMPLATE.format(**fields), subtype="html")
    return msg


def connect():
    """Open an authenticated SMTP connection."""
    if USE_SSL:
        ctx = ssl.create_default_context()
        smtp = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx)
    else:
        smtp = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
        smtp.ehlo()
        smtp.starttls(context=ssl.create_default_context())
        smtp.ehlo()
    smtp.login(SMTP_USER, SMTP_PASSWORD)
    return smtp


def send_all():
    csv_path = Path(CSV_FILE)
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path.resolve()}")

    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))

    if not reader:
        log.warning("CSV is empty, nothing to send.")
        return

    if EMAIL_COLUMN not in reader[0]:
        raise KeyError(f"CSV must contain a '{EMAIL_COLUMN}' column")

    log.info("Sending to %d recipients via %s:%s", len(reader), SMTP_HOST, SMTP_PORT)

    sent, failed = 0, 0
    with open(LOG_FILE, "w", newline="", encoding="utf-8") as logf:
        writer = csv.writer(logf)
        writer.writerow(["email", "status", "error"])

        smtp = connect()
        try:
            for i, row in enumerate(reader, 1):
                to_addr = (row.get(EMAIL_COLUMN) or "").strip()
                if not to_addr:
                    log.warning("Row %d: missing email, skipping", i)
                    writer.writerow(["", "skipped", "no email"])
                    continue

                try:
                    msg = build_message(row)
                    smtp.send_message(msg)
                    log.info("[%d/%d] sent to %s", i, len(reader), to_addr)
                    writer.writerow([to_addr, "sent", ""])
                    sent += 1
                except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError):
                    log.warning("Connection dropped, reconnecting...")
                    smtp = connect()
                    smtp.send_message(msg)
                    writer.writerow([to_addr, "sent (reconnect)", ""])
                    sent += 1
                except Exception as e:
                    log.error("Failed for %s: %s", to_addr, e)
                    writer.writerow([to_addr, "failed", str(e)])
                    failed += 1

                time.sleep(THROTTLE_SECONDS)
        finally:
            try:
                smtp.quit()
            except Exception:
                pass

    log.info("Done. Sent: %d  Failed: %d  Log: %s", sent, failed, LOG_FILE)


if __name__ == "__main__":
    send_all()