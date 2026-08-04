"""
LinkedIn enrichment via the Mindcase API (https://mindcase.co).

Flow for every agent: start a run -> poll the job -> fetch the results. See
linkedin_scrape.py for the standalone CLI version of the same calls.

Configured with MINDCASE_API_KEY. Without it every call here is a no-op, so the
app runs fine with enrichment simply switched off.
"""

import json
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

API_BASE = os.environ.get('MINDCASE_API_BASE', 'https://api.mindcase.co/api/v1')

POLL_INTERVAL_S = 5
MAX_POLLS = 60          # ~5 minutes per job
REQUEST_TIMEOUT_S = 60


def _api_key() -> str | None:
    return (os.environ.get('MINDCASE_API_KEY') or os.environ.get('MINDCASE_KEY') or '').strip() or None


def is_configured() -> bool:
    return _api_key() is not None


# ── Transport ────────────────────────────────────────────────────────────────

def _api(path: str, method: str = 'GET', payload: dict | None = None) -> dict:
    """Call the Mindcase API, backing off once on a 429 (the limit is 60/min)."""
    key = _api_key()
    if not key:
        raise RuntimeError('MINDCASE_API_KEY is not set')
    headers = {'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}
    for attempt in range(2):
        resp = requests.request(method, f'{API_BASE}{path}', headers=headers,
                                json=payload, timeout=REQUEST_TIMEOUT_S)
        if resp.status_code == 429 and attempt == 0:
            logger.warning('[linkedin] rate limited on %s — waiting 30s', path)
            time.sleep(30)
            continue
        if not resp.ok:
            raise RuntimeError(f'Mindcase API error {resp.status_code} on {path}: {resp.text[:300]}')
        return resp.json()
    raise RuntimeError('Unreachable')


def _start_run(agent_path: str, params: dict) -> str:
    data = _api(f'/agents/{agent_path}/run', method='POST', payload={'params': params})
    job_id = data.get('job_id')
    if not job_id:
        raise RuntimeError(f'No job_id in Mindcase response: {json.dumps(data)[:300]}')
    return job_id


def _wait_for_job(job_id: str, max_polls: int = MAX_POLLS) -> dict:
    for _ in range(max_polls):
        job = _api(f'/jobs/{job_id}')
        status = job.get('status')
        if status == 'completed':
            return job
        if status in ('failed', 'error'):
            raise RuntimeError(f'Mindcase job {job_id} failed: {json.dumps(job)[:300]}')
        time.sleep(POLL_INTERVAL_S)
    raise RuntimeError(f'Timed out waiting for Mindcase job {job_id}')


def _results(job_id: str) -> list:
    data = _api(f'/jobs/{job_id}/results')
    rows = data.get('data', data)
    return rows if isinstance(rows, list) else []


def _run_agent(agent_path: str, params: dict, max_polls: int = MAX_POLLS) -> list:
    """Start an agent, wait for it, return its rows."""
    job_id = _start_run(agent_path, params)
    _wait_for_job(job_id, max_polls=max_polls)
    return _results(job_id)


def balance() -> float | None:
    """Remaining Mindcase credit in USD, or None if it can't be read."""
    try:
        return _api('/balance').get('usd_remaining')
    except Exception:
        return None


# ── Field helpers (API field names vary between rows) ────────────────────────

def _pick(obj, keys: list[str]):
    if not isinstance(obj, dict):
        return None
    for k in keys:
        if obj.get(k) is not None:
            return obj[k]
    return None


def profile_url_of(row: dict) -> str | None:
    return _pick(row, ['profileUrl', 'url', 'linkedinUrl', 'publicProfileUrl', 'input', 'query'])


def normalize_url(url: str | None) -> str:
    """Canonical form used to match a scraped row back to the URL we asked for:
    lowercase, no scheme, no www, no trailing slash, no query string."""
    if not url:
        return ''
    u = str(url).strip().lower().split('?')[0].split('#')[0]
    for prefix in ('https://', 'http://'):
        if u.startswith(prefix):
            u = u[len(prefix):]
    if u.startswith('www.'):
        u = u[4:]
    return u.rstrip('/')


def _country(row: dict):
    direct = _pick(row, ['country', 'countryName', 'geoCountryName'])
    if direct:
        return direct
    loc = _pick(row, ['location', 'geoLocationName', 'locationName'])
    if isinstance(loc, str) and ',' in loc:
        return loc.split(',')[-1].strip()
    return loc


def _experience(row: dict) -> list[dict]:
    exp = _pick(row, ['experience', 'experiences', 'positions', 'workExperience']) or []
    if not isinstance(exp, list):
        return []
    return [{
        'title': _pick(e, ['title', 'position', 'jobTitle']),
        'company': _pick(e, ['company', 'companyName', 'organization']),
        'startDate': _pick(e, ['startDate', 'start', 'dateStart']),
        'endDate': _pick(e, ['endDate', 'end', 'dateEnd']),
        'duration': _pick(e, ['duration', 'tenure']),
        'location': _pick(e, ['location', 'locationName']),
        'description': _pick(e, ['description', 'summary']),
    } for e in exp if isinstance(e, dict)]


def _education(row: dict) -> list[dict]:
    edu = _pick(row, ['education', 'educations', 'schools']) or []
    if not isinstance(edu, list):
        return []
    return [{
        'school': _pick(e, ['school', 'schoolName', 'institution']),
        'degree': _pick(e, ['degree', 'degreeName']),
        'field': _pick(e, ['field', 'fieldOfStudy']),
        'startDate': _pick(e, ['startDate', 'start', 'startYear']),
        'endDate': _pick(e, ['endDate', 'end', 'endYear']),
    } for e in edu if isinstance(e, dict)]


# ── Fetching ─────────────────────────────────────────────────────────────────

def fetch_profiles(urls: list[str]) -> dict[str, dict]:
    """Scrape several profiles in one Mindcase run.

    One agent run per batch rather than per person: the API bills per row either
    way, but a batch costs one job and one poll loop instead of N of each.

    Returns {normalized_url: profile_row} for the URLs that came back. Callers
    must treat a missing key as "nothing found for that person".
    """
    urls = [u for u in (urls or []) if u]
    if not urls or not is_configured():
        return {}

    rows = _run_agent('linkedin/profiles', {'queries': urls})
    wanted = {normalize_url(u): u for u in urls}
    out: dict[str, dict] = {}

    for row in rows:
        if not isinstance(row, dict):
            continue
        key = normalize_url(profile_url_of(row))
        if key not in wanted:
            # Some rows echo the query rather than the resolved profile URL —
            # fall back to any wanted URL contained in the row's own values.
            key = next((k for k in wanted if k and k in normalize_url(json.dumps(row))), None)
            if not key:
                continue
        out.setdefault(key, row)

    # A single-URL run that returned exactly one row is unambiguous even when
    # the URL didn't match (redirects, vanity-name changes).
    if not out and len(urls) == 1 and len(rows) == 1 and isinstance(rows[0], dict):
        out[normalize_url(urls[0])] = rows[0]
    return out


def fetch_profile(url: str) -> dict | None:
    """One profile. Returns the raw row, or None when nothing came back."""
    if not url or not is_configured():
        return None
    return fetch_profiles([url]).get(normalize_url(url))


# ── Prompt rendering ─────────────────────────────────────────────────────────

def _fmt_dates(start, end) -> str:
    start, end = (start or ''), (end or '')
    if not start and not end:
        return ''
    return f" ({start}–{end or 'present'})".replace('–)', ')')


def profile_summary(profile: dict, max_experience: int = 6, max_education: int = 3) -> str:
    """The scraped profile as compact prompt lines. Empty string when there is
    nothing worth saying."""
    if not isinstance(profile, dict) or not profile:
        return ''

    name = _pick(profile, ['fullName', 'name']) or ' '.join(
        p for p in [(profile.get('firstName') or '').strip(),
                    (profile.get('lastName') or '').strip()] if p)
    headline = _pick(profile, ['headline', 'title'])
    company = _pick(profile, ['company', 'companyName', 'currentCompany'])
    location = _pick(profile, ['location', 'geoLocationName', 'locationName'])
    country = _country(profile)
    about = _pick(profile, ['about', 'summary', 'bio'])

    lines = []
    if name:
        lines.append(f"- Name: {name}")
    if headline:
        lines.append(f"- Headline: {headline}")
    if company:
        lines.append(f"- Current company: {company}")
    if location or country:
        lines.append(f"- Location: {location or country}")
    if about:
        text = str(about).strip().replace('\n', ' ')
        lines.append(f"- About: {text[:600]}")

    experience = _experience(profile)[:max_experience]
    if experience:
        lines.append('- Career history:')
        for e in experience:
            title = e.get('title') or 'Role'
            at = f" at {e['company']}" if e.get('company') else ''
            lines.append(f"  • {title}{at}{_fmt_dates(e.get('startDate'), e.get('endDate'))}")

    education = _education(profile)[:max_education]
    if education:
        lines.append('- Education:')
        for e in education:
            degree = ', '.join(p for p in [e.get('degree'), e.get('field')] if p)
            school = e.get('school') or 'Unknown'
            lines.append(f"  • {school}{f' — {degree}' if degree else ''}")

    skills = _pick(profile, ['skills', 'topSkills'])
    if isinstance(skills, list) and skills:
        names = [s if isinstance(s, str) else _pick(s, ['name', 'skill', 'title']) for s in skills[:12]]
        names = [n for n in names if n]
        if names:
            lines.append(f"- Skills: {', '.join(names)}")

    return '\n'.join(lines)
