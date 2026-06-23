"""
Delete a list of users from an AWS Cognito User Pool.

Usage:
    python delete_cognito_users.py --user-pool-id us-east-1_XXXXXXXXX --file users.txt
    python delete_cognito_users.py --user-pool-id us-east-1_XXXXXXXXX --file users.txt --dry-run

users.txt format: one username (or email, if your pool uses email as username) per line.
"""

import argparse
import logging
import pdb
import sys
import time
from typing import List

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def load_usernames(path):
    with open(path, "r", encoding="utf-8") as f:
        usernames = [line.strip() for line in f if line.strip()]
    # de-duplicate while preserving order
    seen = set()
    unique = []
    for u in usernames:
        if u not in seen:
            seen.add(u)
            unique.append(u)
    return unique


def delete_user(client, user_pool_id: str, username: str, max_retries: int = 5) -> str:
    """
    Delete a single user with exponential backoff on throttling.
    Returns one of: 'deleted', 'not_found', 'failed'.
    """
    delay = 1.0
    for attempt in range(1, max_retries + 1):
        try:
            client.admin_delete_user(UserPoolId=user_pool_id, Username=username)
            return "deleted"
        except client.exceptions.UserNotFoundException:
            return "not_found"
        except client.exceptions.TooManyRequestsException:
            logger.warning(
                "Throttled on %s (attempt %d/%d). Sleeping %.1fs",
                username, attempt, max_retries, delay,
            )
            time.sleep(delay)
            delay *= 2
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "Unknown")
            logger.error("Failed to delete %s: %s", username, code)
            return "failed"
    logger.error("Gave up on %s after %d retries", username, max_retries)
    return "failed"


def main():
    parser = argparse.ArgumentParser(description="Bulk delete Cognito users.")
    parser.add_argument("--user-pool-id", required=True, help="Cognito User Pool ID")
    parser.add_argument("--file", required=True, help="Path to file with one username per line")
    parser.add_argument("--region", default=None, help="AWS region (optional; uses default if omitted)")
    parser.add_argument("--profile", default=None, help="AWS profile (optional)")
    parser.add_argument("--dry-run", action="store_true", help="List users without deleting")
    parser.add_argument("--rate", type=float, default=0.05,
                        help="Seconds to sleep between deletes (default: 0.05 = ~20 req/s)")
    args = parser.parse_args()
    usernames = load_usernames(args.file)
    if not usernames:
        logger.error("No usernames found in %s", args.file)
        sys.exit(1)

    logger.info("Loaded %d unique usernames", len(usernames))

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    client = session.client("cognito-idp")

    if args.dry_run:
        logger.info("DRY RUN — no users will be deleted")
        for u in usernames:
            print(u)
        logger.info("Would delete %d users", len(usernames))
        return

    counts = {"deleted": 0, "not_found": 0, "failed": 0}
    failed_users = []

    for i, username in enumerate(usernames, 1):
        result = delete_user(client, args.user_pool_id, username)
        counts[result] += 1
        if result == "deleted":
            logger.info("[%d/%d] Deleted: %s", i, len(usernames), username)
        elif result == "not_found":
            logger.warning("[%d/%d] Not found: %s", i, len(usernames), username)
        else:
            failed_users.append(username)

        time.sleep(args.rate)

    logger.info("Done. Deleted=%d, NotFound=%d, Failed=%d",
                counts["deleted"], counts["not_found"], counts["failed"])

    if failed_users:
        with open("failed_users.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(failed_users))
        logger.info("Wrote failed usernames to failed_users.txt")


if __name__ == "__main__":
    main()