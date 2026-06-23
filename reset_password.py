"""
Custom password reset flow for AWS Cognito with SMTP email delivery.

Flow:
  1. request_reset(email) -> generates OTP, stores in DynamoDB, emails via SMTP
  2. confirm_reset(email, otp, new_password) -> verifies OTP, sets password in Cognito
"""

import os
import ssl
import smtplib
import secrets
import hashlib
import time
import logging
from email.message import EmailMessage
from typing import Optional
from botocore.exceptions import ClientError
import boto3

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# --- AWS config ---
AWS_REGION     = os.environ.get("AWS_REGION", "ap-south-1")
USER_POOL_ID   = os.environ["ap-south-1_hEhM0YHEw"]
OTP_TABLE_NAME = os.environ.get("OTP_TABLE_NAME", "password-reset-otps")

# --- SMTP config (put secrets in env vars / Secrets Manager, not in code) ---
SMTP_HOST      = os.environ["SMTP_HOST"]                # e.g. smtp.gmail.com, smtp.sendgrid.net
SMTP_PORT      = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME  = os.environ["SMTP_USERNAME"]
SMTP_PASSWORD  = os.environ["SMTP_PASSWORD"]            # app password or API key
SMTP_USE_TLS   = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"   # STARTTLS on 587
SMTP_USE_SSL   = os.environ.get("SMTP_USE_SSL", "false").lower() == "true"  # implicit SSL on 465
SMTP_FROM      = os.environ["SMTP_FROM"]                # "YourApp <noreply@yourapp.com>"

APP_NAME        = os.environ.get("APP_NAME", "YourApp")
OTP_TTL_SECONDS = 600
MAX_ATTEMPTS    = 5

cognito = boto3.client("cognito-idp", region_name=AWS_REGION)
ddb     = boto3.resource("dynamodb",   region_name=AWS_REGION)
otp_tbl = ddb.Table(OTP_TABLE_NAME)


# ---------- Helpers ----------

def _generate_otp(length: int = 6) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(length))


def _hash_otp(otp: str, email: str) -> str:
    return hashlib.sha256(f"{email}:{otp}".encode()).hexdigest()


def _user_exists(email: str) -> bool:
    try:
        cognito.admin_get_user(UserPoolId=USER_POOL_ID, Username=email)
        return True
    except cognito.exceptions.UserNotFoundException:
        return False


# ---------- SMTP email ----------

def _build_message(to_address: str, otp: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = f"{APP_NAME} password reset code"
    msg["From"]    = SMTP_FROM
    msg["To"]      = to_address

    text_body = (
        f"Your password reset code is: {otp}\n\n"
        f"It expires in {OTP_TTL_SECONDS // 60} minutes. "
        f"If you didn't request this, ignore this email."
    )
    html_body = f"""\
    <html><body style="font-family:Arial,sans-serif;max-width:560px;margin:auto;padding:24px;">
      <h2 style="color:#1a1a1a;">Reset your {APP_NAME} password</h2>
      <p>Use the code below to finish resetting your password:</p>
      <div style="font-size:28px;letter-spacing:6px;font-weight:bold;
                  background:#f4f4f7;padding:16px;text-align:center;border-radius:8px;">
        {otp}
      </div>
      <p style="color:#666;font-size:13px;margin-top:20px;">
        This code expires in {OTP_TTL_SECONDS // 60} minutes.
        If you didn't request a reset, you can safely ignore this email.
      </p>
    </body></html>
    """
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    return msg


def _send_otp_email(to_address: str, otp: str) -> None:
    msg = _build_message(to_address, otp)
    context = ssl.create_default_context()

    if SMTP_USE_SSL:
        # Implicit TLS — typically port 465
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=15) as server:
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
    else:
        # Plain or STARTTLS — typically port 587
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.ehlo()
            if SMTP_USE_TLS:
                server.starttls(context=context)
                server.ehlo()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)


# ---------- Public API ----------

def request_reset(email: str) -> dict:
    email = email.strip().lower()

    # Don't leak whether a user exists.
    if not _user_exists(email):
        logger.info("Reset requested for unknown user: %s", email)
        return {"status": "ok", "message": "If the account exists, a code has been sent."}

    otp = _generate_otp()
    now = int(time.time())

    otp_tbl.put_item(Item={
        "email":      email,
        "otp_hash":   _hash_otp(otp, email),
        "expires_at": now + OTP_TTL_SECONDS,
        "attempts":   0,
        "created_at": now,
    })

    try:
        _send_otp_email(email, otp)
    except (smtplib.SMTPException, OSError) as e:
        logger.error("SMTP send failed for %s: %s", email, e)
        # Roll back the OTP record so the user can retry cleanly.
        otp_tbl.delete_item(Key={"email": email})
        return {"status": "error", "message": "Could not send email. Please try again."}

    return {"status": "ok", "message": "If the account exists, a code has been sent."}


def confirm_reset(email: str, otp: str, new_password: str) -> dict:
    email = email.strip().lower()

    resp = otp_tbl.get_item(Key={"email": email})
    record = resp.get("Item")
    if not record:
        return {"status": "error", "message": "Invalid or expired code."}

    if int(time.time()) > record["expires_at"]:
        otp_tbl.delete_item(Key={"email": email})
        return {"status": "error", "message": "Invalid or expired code."}

    if record["attempts"] >= MAX_ATTEMPTS:
        otp_tbl.delete_item(Key={"email": email})
        return {"status": "error", "message": "Too many attempts. Request a new code."}

    if _hash_otp(otp, email) != record["otp_hash"]:
        otp_tbl.update_item(
            Key={"email": email},
            UpdateExpression="SET attempts = attempts + :one",
            ExpressionAttributeValues={":one": 1},
        )
        return {"status": "error", "message": "Invalid or expired code."}

    try:
        cognito.admin_set_user_password(
            UserPoolId=USER_POOL_ID,
            Username=email,
            Password=new_password,
            Permanent=True,
        )
    except cognito.exceptions.InvalidPasswordException as e:
        return {"status": "error", "message": f"Password does not meet policy: {e.response['Error']['Message']}"}
    except ClientError as e:
        logger.error("admin_set_user_password failed for %s: %s", email, e)
        return {"status": "error", "message": "Could not reset password. Try again."}

    # Revoke existing sessions.
    try:
        cognito.admin_user_global_sign_out(UserPoolId=USER_POOL_ID, Username=email)
    except ClientError as e:
        logger.warning("global sign-out failed for %s: %s", email, e)

    otp_tbl.delete_item(Key={"email": email})
    return {"status": "ok", "message": "Password reset successful."}


# ---------- CLI demo ----------

if __name__ == "__main__":
    import argparse, getpass
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("request"); r.add_argument("email")
    c = sub.add_parser("confirm"); c.add_argument("email"); c.add_argument("otp")
    args = p.parse_args()

    if args.cmd == "request":
        print(request_reset(args.email))
    else:
        pwd = getpass.getpass("New password: ")
        print(confirm_reset(args.email, args.otp, pwd))