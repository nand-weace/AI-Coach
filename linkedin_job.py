"""
LinkedIn enrichment backfill.

A user's LinkedIn URL is recorded at login, but the scrape that follows can be
skipped, come back empty, or fail — and the URL an admin types in later has no
login to hang off at all. This job sweeps up whatever is left: every user whose
user_settings row has a linkedin_url but no linkedin_profile.

Runs on a schedule (every 4 hours, wired up in app.py) and on demand from the
weace_super_admin panel.
"""

import logging
import os
import threading
import time
from datetime import datetime, timezone

import linkedin
from database import (
    count_users_pending_linkedin,
    get_users_pending_linkedin,
    save_user_linkedin_profile,
)

logger = logging.getLogger(__name__)

# Mindcase bills per row, so a run is capped rather than unbounded — a backlog
# larger than this is worked through over successive runs.
BATCH_LIMIT = int(os.environ.get('LINKEDIN_JOB_LIMIT', '50'))

# Profiles per Mindcase agent run. One job for many URLs instead of one each.
CHUNK_SIZE = int(os.environ.get('LINKEDIN_JOB_CHUNK', '10'))


# ── Run state (shared with the admin panel) ──────────────────────────────────

_lock = threading.Lock()
_running = False
_last_run: dict | None = None


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def is_running() -> bool:
    return _running


def last_run() -> dict | None:
    return _last_run


def job_status() -> dict:
    """What the admin panel shows: configuration, backlog size and last result."""
    try:
        counts = count_users_pending_linkedin()
    except Exception as e:
        logger.error('[linkedin-job] could not count pending users: %s', e)
        counts = {'with_url': 0, 'pending': 0, 'enriched': 0}
    return {
        'configured': linkedin.is_configured(),
        'running': _running,
        'last_run': _last_run,
        'limit': BATCH_LIMIT,
        'interval_hours': 4,
        **counts,
    }


# ── The job ──────────────────────────────────────────────────────────────────

def run_linkedin_backfill_job(limit: int = BATCH_LIMIT,
                              skip_user_ids=None,
                              trigger: str = 'scheduled') -> dict:
    """Scrape every pending user's LinkedIn profile and store it.

    `skip_user_ids` is a set of users the app already has a scrape running for,
    so the job never pays for a profile twice. Returns a summary dict, which is
    also kept as the last-run record for the admin panel.

    Only ever one run at a time — a second caller gets a 'skipped' summary back
    rather than a parallel run against the same backlog.
    """
    global _running, _last_run

    with _lock:
        if _running:
            logger.info('[linkedin-job] already running — %s trigger ignored', trigger)
            return {'ok': False, 'skipped': True, 'reason': 'A backfill is already running',
                    'trigger': trigger}
        _running = True

    started_at = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    summary = {
        'ok': True, 'trigger': trigger,
        'started_at': started_at.strftime('%Y-%m-%d %H:%M:%S UTC'),
        'attempted': 0, 'succeeded': 0, 'empty': 0, 'failed': 0, 'remaining': 0,
    }

    try:
        if not linkedin.is_configured():
            summary.update(ok=False, reason='LinkedIn enrichment is not configured (MINDCASE_API_KEY)')
            logger.warning('[linkedin-job] not configured — nothing to do')
            return summary

        pending = get_users_pending_linkedin(limit=limit)
        skip = set(skip_user_ids or ())
        pending = [p for p in pending if p['user_id'] not in skip and p['linkedin_url']]

        logger.info('[linkedin-job] %s run: %s user(s) to enrich', trigger, len(pending))
        summary['attempted'] = len(pending)

        for chunk in _chunks(pending, CHUNK_SIZE):
            urls = [p['linkedin_url'] for p in chunk]
            try:
                found = linkedin.fetch_profiles(urls)
            except Exception as e:
                # A whole chunk failing is a transport/API problem, not a bad
                # profile — count it and move on to the next chunk. Nothing is
                # written, so these users stay in the backlog for the next run.
                logger.error('[linkedin-job] chunk of %s failed: %s', len(chunk), e)
                summary['failed'] += len(chunk)
                continue

            for p in chunk:
                profile = found.get(linkedin.normalize_url(p['linkedin_url']))
                if not profile:
                    summary['empty'] += 1
                    continue
                _save(p['user_id'], profile, summary)

        try:
            summary['remaining'] = count_users_pending_linkedin().get('pending', 0)
        except Exception:
            pass

    except Exception as e:
        logger.exception('[linkedin-job] run failed: %s', e)
        summary.update(ok=False, reason=str(e))
    finally:
        summary['duration_s'] = round(time.perf_counter() - t0, 1)
        summary['finished_at'] = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        _last_run = summary
        _running = False
        logger.info('[linkedin-job] done: %s attempted, %s ok, %s empty, %s failed in %ss',
                    summary['attempted'], summary['succeeded'], summary['empty'],
                    summary['failed'], summary['duration_s'])

    return summary


def _save(user_id: str, profile: dict, summary: dict):
    """Store one scraped profile and count it. A storage failure is logged and
    counted rather than aborting the run."""
    try:
        save_user_linkedin_profile(user_id, profile)
    except Exception as e:
        logger.error('[linkedin-job] could not save result for user_id=%s: %s', user_id, e)
        summary['failed'] += 1
        return
    summary['succeeded'] += 1


if __name__ == '__main__':
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    result = run_linkedin_backfill_job(trigger='cli')
    print(result)
