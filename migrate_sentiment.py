#!/usr/bin/env python3
"""
One-time migration: populates the new sentiment and sentiment_score tables
from existing message data.

What it does:
  1. Runs init_db() to create the new tables and seed the 8 sentiment dimensions.
  2. For every org, scores each user's full message history via the LLM and
     inserts one sentiment_score row per dimension per user.
  3. Advances the sentiment_analysis_cursor to the current max message_id so
     the next scheduled job processes only new messages.

Run once after deploying the new code:
    python migrate_sentiment.py

The old org_sentiment and org_sentiment_history tables are left untouched.
"""

import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from database import (
    init_db,
    get_all_org_slugs,
    get_org_messages_by_user,
    get_org_max_message_id,
    get_sentiment_cursor,
    update_sentiment_cursor,
    insert_user_sentiment_scores,
)
from sentiment_job import _build_client, _score_user

_MAX_USERS_PER_ORG = 30


def migrate():
    print("Step 1: Initialising DB (creates new tables + seeds sentiment dimensions)...")
    init_db()
    print("  Done.\n")

    ai_provider = os.environ.get('AI_PROVIDER', 'claude').lower()
    api_key = os.environ.get('CLAUDE_API_KEY' if ai_provider == 'claude' else 'OPENAI_API_KEY')
    if not api_key:
        print("ERROR: No API key found. Set CLAUDE_API_KEY or OPENAI_API_KEY in your .env file.")
        sys.exit(1)
    ai_model = os.environ.get('AI_MODEL', 'claude-sonnet-4-6' if ai_provider == 'claude' else 'gpt-4o')
    client = _build_client(ai_provider, api_key)

    slugs = get_all_org_slugs()
    print(f"Step 2: Scoring users across {len(slugs)} org(s)...\n")

    total_users = 0
    total_skipped = 0

    for slug in slugs:
        cursor = get_sentiment_cursor(slug)
        if cursor > 0:
            print(f"  [{slug}] Cursor already set ({cursor}), skipping — run already migrated.")
            continue

        user_map = get_org_messages_by_user(slug)
        if not user_map:
            print(f"  [{slug}] No messages found, skipping.")
            continue

        user_ids = list(user_map.keys())
        if len(user_ids) > _MAX_USERS_PER_ORG:
            step = len(user_ids) / _MAX_USERS_PER_ORG
            user_ids = [user_ids[int(i * step)] for i in range(_MAX_USERS_PER_ORG)]
            print(f"  [{slug}] {len(user_map)} users — sampling {len(user_ids)}.")
        else:
            print(f"  [{slug}] Scoring {len(user_ids)} user(s)...")

        run_ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        scored = 0

        for uid in user_ids:
            messages = user_map.get(uid, [])
            scores = _score_user(messages, client, ai_provider, ai_model)
            if scores:
                insert_user_sentiment_scores(uid, scores, run_ts)
                scored += 1
            else:
                total_skipped += 1

        max_id = get_org_max_message_id(slug)
        if max_id:
            update_sentiment_cursor(slug, max_id)

        print(f"  [{slug}] Scored {scored} user(s), cursor set to message_id={max_id}.")
        total_users += scored

    print(f"\nMigration complete: {total_users} user(s) scored, {total_skipped} skipped.")


if __name__ == '__main__':
    migrate()
