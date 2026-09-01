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
RATER_NAME_COLUMN = "Rater Name"
RATER_ROLE_COLUMN = "Rater Role"
PARTICIPANT_NAME_COLUMN = "Participant Name"
EMAIL_TYPE_COLUMN = "Email Type"  # which CSV column holds the email type (assessment or reminder)

SUBJECT_TEMPLATE_SELF = "BPCL eXcelerator: 360° Leadership Assessment Link"
SUBJECT_TEMPLATE_RATER = "360° Leadership Feedback Requested – {participant_name}"

SUBJECT_TEMPLATE_SELF_REMINDER = "Reminder: Complete Your 360° Leadership Assessment"
SUBJECT_TEMPLATE_RATER_REMINDER = "Reminder: Your 360°Feedback is Requested"


BODY_TEMPLATE = """\
Dear {name},

Please find below the link to access your 360° leadership assessment:

Click on the link below to begin your 360°-assessment.

360 Assessment: {assessment_link}

Before you start, kindly ensure that you:

· Read the instructions carefully.

· Complete all required questions before submitting the 360°-assessment.

· Have approximately 10 minutes available to complete the 360°-assessment.

· Click submit once you have completed the 360°-assessment.

360°-assessment closing date: <b>7th September 2026</b>

Please note that no login credentials are required to access the assessment.

If you face any difficulty accessing the assessment or have any queries, please write to leadership.development@emeritus.org. Our team will respond to your query within 24 hours.

We wish you all the very best as you continue your leadership development journey.

Warm Regards, Team Emeritus
"""

# Optional HTML version (set to None to send plain text only)
HTML_TEMPLATE_SELF = """\
<p><b>Dear {name},</b></p>

<p>Please find below the link to access your 360&deg; Leadership Assessment:</p>

<p>Click on the link below to begin your 360&deg;Leadership Assessment </p>
<b><a href="{assessment_link}">360&deg;Leadership Assessment</a></b>

<p>Before you start, kindly ensure that you:</p>

<ul>
  <li>Read the instructions carefully.</li>
  <li>Complete all required questions before submitting the 360&deg;-assessment.</li>
  <li>Have approximately 10 minutes available to complete the 360&deg;-assessment.</li>
  <li>Click submit once you have completed the 360&deg;-assessment.</li>
</ul>

<p><b>360&deg;-assessment closing date: 7th September 2026</b></p>

<p>Please note that no login credentials are required to access the assessment.</p>

<p>If you face any difficulty accessing the assessment or have any queries, please write to
<a href="mailto:leadership.development@emeritus.org">leadership.development@emeritus.org</a>.
Our team will respond to your query within 24 hours.</p>

<p>We wish you all the very best as you continue your leadership development journey.</p>

<p>Warm Regards,<br>Team Emeritus</p>
"""

HTML_TEMPLATE_RATER = """\
<p><b>Dear {name},</b></p>

<p> As part of the BPCL eXcelerator Leadership Development Programme, {participant_name} has been nominated to undergo a 360° Leadership Assessment. </p>

<p>You have been identified as a {rater_role} for this 360-assessment, and your candid and constructive feedback will play an important role in supporting their leadership development journey. Your feedback remains confidential.  </p>

<p><b>We request you to complete the 360°-assessment by 7th September 2026. </b></p>

<p>Click on the link below to begin your 360°-assessment feedback </p>

<p><a href="{assessment_link}">360&deg;Leadership Assessment</a></p>

<p>If you face any difficulty accessing the assessment or have any queries, please write to
<a href="mailto:leadership.development@emeritus.org">leadership.development@emeritus.org</a>.
Our team will respond to your query within 24 hours.</p>


Thank you for taking the time to provide thoughtful and meaningful feedback. <br/>

<p><b>Warm Regards,<br>Team Emeritus</b></p>
"""

HTML_TEMPLATE_RATER_REMINDER = """\
<p><b>Dear {name},</b></p>

<p> A gentle reminder to complete your <b> 360° Feedback for {participant_name} </b> </p>

<p>Your feedback is an important input to support the participant’s leadership development journey. </p>

<p><b>360°-assessment closing date 7th September 2026. </b></p>

<p>Click on the link below to begin your 360°-assessment feedback </p>

<p><a href="{assessment_link}">360&deg;Leadership Assessment</a></p>

<p>If you face any difficulty accessing the assessment or have any queries, please write to
<a href="mailto:leadership.development@emeritus.org">leadership.development@emeritus.org</a>.
Our team will respond to your query within 24 hours.</p>


Thank you for taking the time to provide thoughtful and meaningful feedback. <br/>

<p><b>Warm Regards,<br>Team Emeritus</b></p>
"""

HTML_TEMPLATE_SELF_REMINDER = """\
<p><b>Dear {name},</b></p>

<p> This is a gentle reminder to complete the 360° leadership assessment.</p>

<p>Click on the link below to begin your 360°assessment feedback</p>

<p><a href="{assessment_link}">360&deg;Leadership Assessment</a></p>

<p><b>360°-assessment closing date 7th September 2026. </b></p>

<p>If you have any questions or require assistance, please write to 
<a href="mailto:leadership.development@emeritus.org">leadership.development@emeritus.org</a>.
Our team will respond to your query within 24 hours.</p> <br/>

<p><b>Warm Regards,<br>Team Emeritus</b></p>
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


def build_message_assessment(to_addr, to_name, assessment_link, rater_role, participant_name) -> EmailMessage:
    """Build an EmailMessage from a CSV row using the configured templates."""
    fields = {
        "name": to_name,
        "from_name": FROM_NAME,
        "assessment_link": assessment_link,
        "rater_role": rater_role,
        "participant_name": participant_name
    }

    msg = EmailMessage()
    msg["From"] = f"{FROM_NAME} <{FROM_ADDRESS}>"
    msg["To"] = to_addr
    if rater_role.lower() == "self":
        msg["Subject"] = SUBJECT_TEMPLATE_SELF.format(**fields)
        msg.add_alternative(HTML_TEMPLATE_SELF.format(**fields), subtype="html")
    else:
        msg["Subject"] = SUBJECT_TEMPLATE_RATER.format(**fields)
        msg.add_alternative(HTML_TEMPLATE_RATER.format(**fields), subtype="html")
    # import ipdb; ipdb.set_trace()
    return msg

def build_message_reminder(to_addr, to_name, assessment_link, rater_role, participant_name) -> EmailMessage:
    """Build an EmailMessage from a CSV row using the configured templates."""
    fields = {
        "name": to_name,
        "from_name": FROM_NAME,
        "assessment_link": assessment_link,
        "rater_role": rater_role,
        "participant_name": participant_name
    }

    msg = EmailMessage()
    msg["From"] = f"{FROM_NAME} <{FROM_ADDRESS}>"
    msg["To"] = to_addr
    if rater_role.lower() == "self":
        msg["Subject"] = SUBJECT_TEMPLATE_SELF_REMINDER.format(**fields)
        msg.add_alternative(HTML_TEMPLATE_SELF_REMINDER.format(**fields), subtype="html")
    else:
        msg["Subject"] = SUBJECT_TEMPLATE_RATER_REMINDER.format(**fields)
        msg.add_alternative(HTML_TEMPLATE_RATER_REMINDER.format(**fields), subtype="html")
    # import ipdb; ipdb.set_trace()
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
                to_name = (row.get(RATER_NAME_COLUMN) or "").strip()
                assessment_link = (row.get(Assessment_LINK) or "").strip()
                rater_role = (row.get(RATER_ROLE_COLUMN) or "").strip()
                participant_name = (row.get(PARTICIPANT_NAME_COLUMN) or "").strip()
                if not to_addr:
                    log.warning("Row %d: missing email, skipping", i)
                    writer.writerow(["", "skipped", "no email"])
                    continue

                try:
                    email_type = row.get(EMAIL_TYPE_COLUMN) or "assessment"
                    if email_type == "reminder":
                        msg = build_message_reminder(to_addr, to_name, assessment_link, rater_role, participant_name)
                    else:
                        msg = build_message_assessment(to_addr, to_name, assessment_link, rater_role, participant_name)
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