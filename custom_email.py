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

from dotenv import ipython

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SMTP_HOST = "smtp.office365.com"
SMTP_PORT = 587                  # 587 = STARTTLS, 465 = SSL
SMTP_USER = "leadership.development@Emeritus.org"
SMTP_PASSWORD = "Zukq9312"   # Use an app password, not your login password
USE_SSL = False                  # True for port 465, False for 587 STARTTLS

FROM_ADDRESS = "leadership.development@Emeritus.org"
FROM_NAME = "Emeritus Leadership Development Team"

CSV_FILE = "BPCL-Assessment.csv"  # path to CSV file with recipients
EMAIL_COLUMN = "Receipient Email ID"           # which CSV column holds the address
Assessment_LINK = "Typeform Link"  # which CSV column holds the assessment link
PARTICIPANT_NAME_COLUMN = "Participant Name"

SUBJECT_TEMPLATE = "BPCL eXcelerator: 360° Leadership Assessment Link"
BODY_TEMPLATE = """\
Dear {name},

Please find below the link to access your 360° leadership assessment:

Click on the link below to begin your 360°-assessment.

360°-assessment link: {assessment_link}

Before you start, kindly ensure that you:

· Read the instructions carefully.

· Complete all required questions before submitting the 360°-assessment.

· Have approximately 10 minutes available to complete the 360°-assessment.

· Click submit once you have completed the 360°-assessment.

360°-assessment closing date: 7th September 2026

Please note that no login credentials are required to access the assessment.

If you face any difficulty accessing the assessment or have any queries, please write to leadership.development@emeritus.org. Our team will respond to your query within 24 hours.

We wish you all the very best as you continue your leadership development journey.

Warm Regards, Team Emeritus
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


def build_message(to_addr, to_name, assessment_link) -> EmailMessage:
    """Build an EmailMessage from a CSV row using the configured templates."""
    fields = {
        "name": to_name,
        "from_name": FROM_NAME,
        "assessment_link": assessment_link
    }

    msg = EmailMessage()
    msg["From"] = f"{FROM_NAME} <{FROM_ADDRESS}>"
    msg["To"] = to_addr
    msg["Subject"] = SUBJECT_TEMPLATE.format(**fields)
    msg.set_content(BODY_TEMPLATE.format(**fields))
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

    log.info("Sending to %d recipients via %s:%s", len(reader), SMTP_HOST, SMTP_PORT)

    sent, failed = 0, 0
    with open(LOG_FILE, "w", newline="", encoding="utf-8") as logf:
        writer = csv.writer(logf)
        writer.writerow(["email", "status", "error"])

        smtp = connect()
        try:
            for i, row in enumerate(reader, 1):
                to_addr = (row.get(EMAIL_COLUMN) or "").strip()
                to_name = (row.get(PARTICIPANT_NAME_COLUMN) or "").strip()
                assessment_link = (row.get(Assessment_LINK) or "").strip()
                if not to_addr:
                    log.warning("Row %d: missing email, skipping", i)
                    writer.writerow(["", "skipped", "no email"])
                    continue

                try:
                    msg = build_message(to_addr, to_name, assessment_link)
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