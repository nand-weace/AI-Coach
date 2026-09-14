"""Calendar context for Nexa.

Pulls the user's events from the WeAce calendar API and turns them into what a
coach would actually want to know before opening a conversation: how loaded
today and this week look, which upcoming meeting matters most, and which recent
one is worth asking about.

The API serves one day per call (`/api/v1/calendar/primary-events?date=...`),
so a window of days is fetched in parallel and merged. The response shape is
not documented anywhere we control, so parsing is deliberately forgiving —
Google-style (`summary`, `start.dateTime`) and flat (`title`, `startTime`)
events are both understood, and the keys of the first event seen are logged so
the mapping can be tightened once real data has been observed.
"""

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

# Days either side of today that make up the context window. The past days are
# what "how did X go?" draws on; the future days are the week's workload.
DAYS_BACK = int(os.getenv('CALENDAR_DAYS_BACK', '3'))
DAYS_AHEAD = int(os.getenv('CALENDAR_DAYS_AHEAD', '7'))

# Timezone used to decide what "today" and "this week" mean when an event does
# not carry its own offset. Most WeAce users are in India.
DEFAULT_TZ = os.getenv('CALENDAR_TZ', 'Asia/Kolkata')

_PER_DAY_TIMEOUT = float(os.getenv('CALENDAR_FETCH_TIMEOUT', '6'))
_MAX_WORKERS = 5

# Meetings that tend to carry weight for a leader — used to score importance
# alongside attendee count and duration. Lower-cased substring matches.
_IMPORTANT_TERMS = (
    'board', 'review', 'performance', 'appraisal', 'promotion', '1:1', '1-1',
    'one on one', 'one-on-one', 'skip level', 'skip-level', 'leadership', 'exec',
    'steering', 'steerco', 'strategy', 'offsite', 'town hall', 'townhall',
    'all hands', 'all-hands', 'client', 'customer', 'pitch', 'negotiation',
    'interview', 'hiring', 'budget', 'quarterly', 'qbr', 'okr', 'planning',
    'kickoff', 'kick-off', 'launch', 'escalation', 'crisis', 'feedback',
    'ceo', 'cfo', 'coo', 'cto', 'chro', 'md ', 'investor', 'partner',
    'presentation', 'demo', 'decision', 'deal', 'retro', 'postmortem',
    'post-mortem', 'restructur', 'reorg', 'coaching', 'mentor',
)

# Calendar noise that should never be surfaced as "an important meeting".
_NOISE_TERMS = (
    'lunch', 'focus time', 'focus block', 'blocked', 'hold', 'ooo',
    'out of office', 'holiday', 'birthday', 'reminder', 'commute', 'gym',
    'no meeting', 'busy', 'dnd', 'do not disturb', 'standup', 'stand-up',
    'daily sync', 'scrum',
)


# --- fetching ------------------------------------------------------------------

def fetch_events(base_url: str, token: str, user_id: str, today: date | None = None,
                 days_back: int = DAYS_BACK, days_ahead: int = DAYS_AHEAD) -> list:
    """Every event in the window, normalised and sorted by start time.

    Failures on individual days are logged and skipped — a partial calendar is
    still useful, and a calendar outage must never break login.
    """
    if not token or not user_id:
        return []
    today = today or datetime.now(ZoneInfo(DEFAULT_TZ)).date()
    days = [today + timedelta(days=d) for d in range(-days_back, days_ahead + 1)]

    raw_events = []
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_day, base_url, token, user_id, d): d for d in days}
        for fut in as_completed(futures):
            try:
                raw_events.extend(fut.result())
            except Exception as e:
                logger.warning("[calendar] fetch failed for user_id=%s date=%s: %s",
                               user_id, futures[fut], e)

    seen = set()
    events = []
    for raw in raw_events:
        ev = normalise_event(raw)
        if not ev:
            continue
        key = ev['id'] or (ev['title'], ev['start'].isoformat())
        if key in seen:
            continue
        seen.add(key)
        events.append(ev)
    events.sort(key=lambda e: e['start'])
    logger.info("[calendar] user_id=%s window=%s..%s raw=%s events=%s",
                user_id, days[0], days[-1], len(raw_events), len(events))
    return events


def _fetch_day(base_url: str, token: str, user_id: str, day: date) -> list:
    resp = requests.get(
        f'{base_url}/api/v1/calendar/primary-events',
        params={'userId': user_id, 'date': day.isoformat(), 'allCalendars': 'true'},
        headers={'accept': 'application/json', 'Authorization': f'Bearer {token}'},
        timeout=_PER_DAY_TIMEOUT,
    )
    if not resp.ok:
        logger.warning("[calendar] primary-events status=%s date=%s body=%r",
                       resp.status_code, day, resp.text[:200])
        return []
    return _collect_events(resp.json())


_logged_shape = False


def _collect_events(payload) -> list:
    """Find the event dicts wherever the API nested them.

    Handles a bare list, `{"data": [...]}`, `{"data": {"events": [...]}}`, and
    `{"data": {"<calendar>": [...]}}` (allCalendars=true may group per calendar)
    by walking the payload for lists of dicts that look like events.
    """
    global _logged_shape
    found = []

    def walk(node, depth=0):
        if depth > 4:
            return
        if isinstance(node, list):
            if node and all(isinstance(x, dict) for x in node) and _looks_like_event(node[0]):
                found.extend(node)
            else:
                for x in node:
                    walk(x, depth + 1)
        elif isinstance(node, dict):
            if _looks_like_event(node) and depth > 0:
                found.append(node)
                return
            for v in node.values():
                walk(v, depth + 1)

    walk(payload)
    if found and not _logged_shape:
        _logged_shape = True
        logger.info("[calendar] first event keys seen: %s", sorted(found[0].keys()))
    return found


def _looks_like_event(d: dict) -> bool:
    keys = {k.lower() for k in d.keys()}
    has_start = bool(keys & {'start', 'starttime', 'start_time', 'startdatetime', 'startdate', 'from'})
    has_name = bool(keys & {'summary', 'title', 'subject', 'name', 'eventname', 'event_name'})
    return has_start and has_name


# --- normalising ---------------------------------------------------------------

def _first(d: dict, *names):
    lower = {k.lower(): v for k, v in d.items()}
    for n in names:
        v = lower.get(n.lower())
        if v not in (None, ''):
            return v
    return None


def _parse_when(value, tz: ZoneInfo):
    """A datetime (aware) from the many ways a calendar API spells one.

    Returns (datetime, all_day). Strings without a time are treated as all-day.
    """
    if value is None:
        return None, False
    if isinstance(value, dict):
        inner = _first(value, 'dateTime', 'date_time', 'datetime', 'date', 'value')
        tzname = _first(value, 'timeZone', 'timezone', 'tz')
        if tzname:
            try:
                tz = ZoneInfo(tzname)
            except Exception:
                pass
        all_day = 'datetime' not in {k.lower() for k in value} and \
                  isinstance(inner, str) and len(inner) <= 10
        dt, _ = _parse_when(inner, tz)
        return dt, all_day
    if isinstance(value, (int, float)):
        ts = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz), False
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, False
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
            return datetime.fromisoformat(text).replace(tzinfo=tz), True
        text = text.replace('Z', '+00:00')
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None, False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(tz), False
    return None, False


def normalise_event(raw: dict, tz: ZoneInfo | None = None) -> dict | None:
    tz = tz or ZoneInfo(DEFAULT_TZ)
    title = _first(raw, 'summary', 'title', 'subject', 'name', 'eventName', 'event_name')
    start, all_day = _parse_when(
        _first(raw, 'start', 'startTime', 'start_time', 'startDateTime', 'startDate', 'from'), tz)
    end, _ = _parse_when(
        _first(raw, 'end', 'endTime', 'end_time', 'endDateTime', 'endDate', 'to'), tz)
    if not title or not start:
        return None
    if not end or end < start:
        end = start + (timedelta(days=1) if all_day else timedelta(hours=1))

    status = str(_first(raw, 'status', 'eventStatus') or '').lower()
    if status in ('cancelled', 'canceled', 'declined'):
        return None
    # The user's own RSVP, if the API exposes it
    my_status = str(_first(raw, 'responseStatus', 'myResponse', 'rsvp') or '').lower()
    if my_status in ('declined',):
        return None

    attendees = _first(raw, 'attendees', 'participants', 'guests', 'members') or []
    if isinstance(attendees, list):
        attendee_names = [_attendee_name(a) for a in attendees]
        attendee_names = [n for n in attendee_names if n]
        attendee_count = len(attendees)
    else:
        attendee_names, attendee_count = [], 0

    organizer = _first(raw, 'organizer', 'organiser', 'host', 'creator')
    organizer = _attendee_name(organizer) if organizer else ''

    return {
        'id': str(_first(raw, 'id', 'eventId', 'event_id', 'iCalUID', '_id') or ''),
        'title': str(title).strip(),
        'start': start,
        'end': end,
        'all_day': all_day,
        'attendee_count': attendee_count,
        'attendees': attendee_names[:8],
        'organizer': organizer,
        'description': str(_first(raw, 'description', 'notes', 'body') or '').strip()[:300],
        'location': str(_first(raw, 'location', 'venue') or '').strip()[:100],
        'calendar': str(_first(raw, 'calendarName', 'calendar', 'calendarId', 'source') or '').strip(),
    }


def _attendee_name(a) -> str:
    if isinstance(a, str):
        return a.split('@')[0] if '@' in a else a
    if isinstance(a, dict):
        return str(_first(a, 'displayName', 'name', 'fullName', 'email', 'emailAddress') or '') \
            .split('@')[0]
    return ''


# --- summarising ---------------------------------------------------------------

def importance(ev: dict) -> float:
    """How much a meeting is likely to matter to this person, 0 = skip."""
    title = ev['title'].lower()
    if ev['all_day'] or any(t in title for t in _NOISE_TERMS):
        return 0.0
    score = 1.0
    score += sum(2.0 for t in _IMPORTANT_TERMS if t in title)
    hours = (ev['end'] - ev['start']).total_seconds() / 3600
    if hours >= 1.5:
        score += 1.5
    elif hours >= 1:
        score += 0.5
    n = ev['attendee_count']
    if n >= 8:
        score += 1.5
    elif n >= 4:
        score += 1.0
    elif n == 2:
        score += 0.5  # a 1:1 in disguise
    if ev['description']:
        score += 0.5
    return score


def _hours(events) -> float:
    return round(sum((e['end'] - e['start']).total_seconds() for e in events
                     if not e['all_day']) / 3600, 1)


def _load_label(day_hours: float, day_count: int) -> str:
    if day_count == 0:
        return 'clear'
    if day_hours >= 6 or day_count >= 8:
        return 'heavy'
    if day_hours >= 3.5 or day_count >= 5:
        return 'busy'
    return 'light'


def summarise(events: list, now: datetime | None = None) -> dict | None:
    """The calendar reduced to what the opener needs. None when there's nothing."""
    if not events:
        return None
    tz = events[0]['start'].tzinfo or ZoneInfo(DEFAULT_TZ)
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    today = now.date()
    week_end = today + timedelta(days=DAYS_AHEAD)

    todays = [e for e in events if e['start'].date() == today]
    remaining_today = [e for e in todays if e['end'] > now]
    upcoming = [e for e in events if e['start'] > now and e['start'].date() <= week_end]
    week_ahead = [e for e in events if today < e['start'].date() <= week_end]
    past = [e for e in events if e['end'] <= now]

    # Busiest day in the coming week — the thing worth a heads-up.
    by_day: dict = {}
    for e in week_ahead:
        by_day.setdefault(e['start'].date(), []).append(e)
    busiest = max(by_day.items(), key=lambda kv: (_hours(kv[1]), len(kv[1])), default=None)

    def pick(candidates, reverse=False):
        scored = [(importance(e), e) for e in candidates]
        scored = [(s, e) for s, e in scored if s >= 3.0]
        if not scored:
            return None
        # Tie-break towards the nearer meeting (soonest upcoming / most recent past)
        scored.sort(key=lambda se: (se[0], se[1]['start'] if reverse else -se[1]['start'].timestamp()),
                    reverse=True)
        return scored[0][1]

    next_important = pick(upcoming)
    recent_important = pick(past, reverse=True)

    day_hours = _hours(todays)
    week_hours = _hours(week_ahead)
    return {
        'now': now,
        'today': today,
        'today_count': len(todays),
        'today_hours': day_hours,
        'today_remaining': len(remaining_today),
        'today_load': _load_label(day_hours, len(todays)),
        'today_events': todays,
        'week_count': len(week_ahead),
        'week_hours': week_hours,
        # Averaged over a working week so a single stacked day doesn't read as
        # a heavy week.
        'week_load': _load_label(week_hours / 5, len(week_ahead) // 5),
        'busiest_day': busiest[0] if busiest else None,
        'busiest_day_hours': _hours(busiest[1]) if busiest else 0,
        'next_important': next_important,
        'recent_important': recent_important,
        'upcoming': upcoming[:12],
        'past': past[-8:],
    }


# --- prompt block --------------------------------------------------------------

HEADER = 'CALENDAR CONTEXT FOR'


def _when(ev: dict, now: datetime) -> str:
    """'today 3:00 PM', 'tomorrow 10:30 AM', 'Thu 18 Sep 9:00 AM', 'yesterday 4 PM'…"""
    d = ev['start'].date()
    delta = (d - now.date()).days
    if delta == 0:
        day = 'today'
    elif delta == 1:
        day = 'tomorrow'
    elif delta == -1:
        day = 'yesterday'
    elif 1 < delta <= 6:
        day = ev['start'].strftime('%A')
    elif -6 <= delta < -1:
        day = 'last ' + ev['start'].strftime('%A')
    else:
        day = ev['start'].strftime('%a %d %b')
    if ev['all_day']:
        return f'{day} (all day)'
    return f"{day} {ev['start'].strftime('%-I:%M %p')}"


def _line(ev: dict, now: datetime) -> str:
    mins = int((ev['end'] - ev['start']).total_seconds() // 60)
    bits = [f"{_when(ev, now)} — \"{ev['title']}\""]
    if not ev['all_day']:
        bits.append(f"{mins} min")
    if ev['attendee_count'] > 1:
        bits.append(f"{ev['attendee_count']} attendees")
    if ev['organizer']:
        bits.append(f"organised by {ev['organizer']}")
    return '• ' + ', '.join(bits)


def block(user_name: str, summary: dict | None) -> str:
    """System-prompt block describing the user's calendar. Empty when unknown."""
    if not summary:
        return ''
    now = summary['now']
    lines = [f"\n\n---\n{HEADER} {user_name.upper()}:",
             f"Now: {now.strftime('%A %d %B %Y, %-I:%M %p')} ({now.tzname()}).",
             f"Today: {summary['today_count']} meeting(s), {summary['today_hours']}h — "
             f"{summary['today_load']} day; {summary['today_remaining']} still to come.",
             f"Next {DAYS_AHEAD} days: {summary['week_count']} meeting(s), {summary['week_hours']}h."]
    if summary['busiest_day']:
        lines.append(f"Busiest day ahead: {summary['busiest_day'].strftime('%A %d %b')} "
                     f"({summary['busiest_day_hours']}h of meetings).")
    if summary['next_important']:
        lines.append("KEY UPCOMING MEETING: " + _line(summary['next_important'], now)[2:])
        if summary['next_important']['description']:
            lines.append(f"  Notes: {summary['next_important']['description']}")
    if summary['recent_important']:
        lines.append("KEY RECENT MEETING: " + _line(summary['recent_important'], now)[2:])
    if summary['upcoming']:
        lines.append("Upcoming:")
        lines.extend(_line(e, now) for e in summary['upcoming'])
    if summary['past']:
        lines.append("Recent:")
        lines.extend(_line(e, now) for e in summary['past'])
    lines.append(
        "---\n"
        "This is their real calendar. Use it the way a coach who knows their week would: "
        "factor the load into how you pace the conversation, help them prepare for what "
        "matters, and ask how a significant meeting went. Refer to meetings by name and "
        "day naturally; never list the calendar back at them, mention hours totals "
        "mechanically, or say you have access to their calendar — you simply know their "
        "week. If they say a meeting moved or was cancelled, believe them."
    )
    return '\n'.join(lines)


def welcome_instruction(summary: dict | None) -> str:
    """What the opener should do with the calendar, given what's in it."""
    if not summary:
        return ''
    nxt, prev = summary['next_important'], summary['recent_important']
    parts = [" You know this person's calendar (see CALENDAR CONTEXT). Make the opener about "
             "their actual week, not generic coaching."]
    if nxt and prev:
        parts.append(
            f" Choose ONE angle: either open on their upcoming \"{nxt['title']}\" "
            f"({_when(nxt, summary['now'])}) — what's at stake and how they want to show up — "
            f"or ask how \"{prev['title']}\" ({_when(prev, summary['now'])}) went. Prefer the "
            "upcoming one if it is within the next two days; otherwise ask about the recent one.")
    elif nxt:
        parts.append(
            f" Open on their upcoming \"{nxt['title']}\" ({_when(nxt, summary['now'])}) — what "
            "it's about, what's at stake, and how they want to show up.")
    elif prev:
        parts.append(
            f" Open by asking how \"{prev['title']}\" ({_when(prev, summary['now'])}) went — "
            "what happened, what they'd do again.")
    load = summary['today_load']
    if load == 'heavy':
        parts.append(" Their day is packed; acknowledge that lightly and keep the opener short.")
    elif load == 'clear' and summary['week_count'] == 0:
        parts.append(" Their calendar is clear — a good moment for deeper reflection.")
    elif summary['busiest_day'] and summary['busiest_day'] != summary['today']:
        parts.append(f" {summary['busiest_day'].strftime('%A')} is their heaviest day this week — "
                     "worth a nod if it fits naturally.")
    parts.append(
        " At least one nudge must be about a specific meeting on their calendar, in their "
        "own words (e.g. \"Help me prep for the board review on Thursday\" or \"Let me "
        "debrief yesterday's client call\").")
    return ''.join(parts)


# --- on-demand lookups (chat tool) ----------------------------------------------
#
# The login-time window covers the coming week. When a user asks about a
# specific date or a wider range ("what does next month look like?", "was I
# free on the 3rd?"), Nexa calls this tool and the answer comes from the API.

MAX_LOOKUP_DAYS = int(os.getenv('CALENDAR_MAX_LOOKUP_DAYS', '31'))

TOOL = {
    "name": "get_calendar",
    "description": (
        "Look up the user's real calendar events for a date range. Use it whenever the "
        "user asks about their schedule, meetings, availability, or workload — for any "
        "day or range not already listed in CALENDAR CONTEXT, or when they want the "
        "full detail of a day. Dates are inclusive, ISO format (YYYY-MM-DD), at most "
        f"{MAX_LOOKUP_DAYS} days per call. Resolve relative dates ('next Monday', "
        "'the 25th') using the current date in CALENDAR CONTEXT."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "start_date": {"type": "string", "description": "First day, YYYY-MM-DD"},
            "end_date": {"type": "string",
                         "description": "Last day, YYYY-MM-DD (defaults to start_date)"},
        },
        "required": ["start_date"],
        "additionalProperties": False,
    },
    "strict": True,
}


def tool_hint() -> str:
    """One line for the system prompt so Nexa knows the lookup exists."""
    return (
        "\n\nYou can look up any day or range of the user's calendar with the get_calendar "
        "tool. Use it when they ask about their schedule, a specific meeting, availability, "
        "or workload for dates you don't already have — don't guess or say you can't see "
        "their calendar. After looking, answer as a coach who knows their week, not as a "
        "calendar readout: mention what matters, and ask about what stands out."
    )


def lookup(base_url: str, token: str, user_id: str, start_date: str,
           end_date: str | None = None, now: datetime | None = None) -> str:
    """Events between two dates, rendered for the model. Never raises."""
    tz = ZoneInfo(DEFAULT_TZ)
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    try:
        start = date.fromisoformat((start_date or '').strip())
        end = date.fromisoformat((end_date or start_date).strip())
    except ValueError:
        return "Error: dates must be YYYY-MM-DD."
    if end < start:
        start, end = end, start
    span = (end - start).days
    if span >= MAX_LOOKUP_DAYS:
        end = start + timedelta(days=MAX_LOOKUP_DAYS - 1)
        span = MAX_LOOKUP_DAYS - 1
    try:
        events = fetch_events(base_url, token, user_id, today=start,
                              days_back=0, days_ahead=span)
    except Exception as e:
        logger.error("[calendar] lookup failed user_id=%s %s..%s: %s", user_id, start, end, e)
        return "The calendar could not be reached right now."
    events = [e for e in events if start <= e['start'].date() <= end]

    label = start.strftime('%a %d %b %Y') if start == end else \
        f"{start.strftime('%a %d %b %Y')} to {end.strftime('%a %d %b %Y')}"
    if not events:
        return f"No events on {label}."
    lines = [f"{len(events)} event(s), {_hours(events)}h of meetings, {label}:"]
    current = None
    for ev in events:
        day = ev['start'].date()
        if day != current:
            current = day
            lines.append(f"\n{day.strftime('%A %d %b')}:")
        lines.append(_line(ev, now))
        if ev['description']:
            lines.append(f"    Notes: {ev['description']}")
        if ev['attendees']:
            lines.append(f"    With: {', '.join(ev['attendees'])}")
    return '\n'.join(lines)
