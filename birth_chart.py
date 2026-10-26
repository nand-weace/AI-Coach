"""Vedic birth chart for the Career Horoscope.

The astrologer prompts are written to read a pre-computed chart rather than
guess one from raw birth details, so this module works it out:

- the place of birth is geocoded (Open-Meteo, no key needed) to coordinates
  and an IANA time zone, which turns the local birth time into UTC with the
  offset that applied on that date;
- planet positions come from Skyfield with the JPL DE421 ephemeris (1900-2050),
  converted to sidereal longitudes with the Lahiri ayanamsa;
- houses are whole-sign from the ascendant, and also counted from the Moon,
  which is what the reading falls back on when the birth time is unknown;
- the full Vimshottari dasha timeline (mahadashas and antardashas) is fixed at
  birth, so it is stored whole and "the current period" becomes a date lookup.

`compute_chart` is the expensive part (a geocoding call plus ephemeris work)
and is meant to run once per set of birth details; the caller stores the
result. `transits` and `prompt_block` are cheap and run per request.
"""

import logging
import math
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

# Bump when the calculation changes, so stored charts are worked out again.
CHART_VERSION = 1

EPHEMERIS_DIR = os.getenv('EPHEMERIS_DIR',
                          os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ephemeris'))
EPHEMERIS_FILE = 'de421.bsp'
GEOCODE_URL = 'https://geocoding-api.open-meteo.com/v1/search'

SIGNS = ('Aries', 'Taurus', 'Gemini', 'Cancer', 'Leo', 'Virgo', 'Libra',
         'Scorpio', 'Sagittarius', 'Capricorn', 'Aquarius', 'Pisces')
SIGN_LORDS = ('Mars', 'Venus', 'Mercury', 'Moon', 'Sun', 'Mercury', 'Venus',
              'Mars', 'Jupiter', 'Saturn', 'Saturn', 'Jupiter')
NAKSHATRAS = ('Ashwini', 'Bharani', 'Krittika', 'Rohini', 'Mrigashira', 'Ardra',
              'Punarvasu', 'Pushya', 'Ashlesha', 'Magha', 'Purva Phalguni',
              'Uttara Phalguni', 'Hasta', 'Chitra', 'Swati', 'Vishakha', 'Anuradha',
              'Jyeshtha', 'Mula', 'Purva Ashadha', 'Uttara Ashadha', 'Shravana',
              'Dhanishta', 'Shatabhisha', 'Purva Bhadrapada', 'Uttara Bhadrapada', 'Revati')
NAKSHATRA_SPAN = 360 / 27

# Vimshottari order and years; Ashwini's lord is Ketu and the cycle repeats.
DASHA_ORDER = (('Ketu', 7), ('Venus', 20), ('Sun', 6), ('Moon', 10), ('Mars', 7),
               ('Rahu', 18), ('Jupiter', 16), ('Saturn', 19), ('Mercury', 17))
DASHA_YEAR_DAYS = 365.25

# Skyfield target names for the bodies read from the ephemeris.
_BODIES = (('Sun', 'sun'), ('Moon', 'moon'), ('Mercury', 'mercury'),
           ('Venus', 'venus'), ('Mars', 'mars'), ('Jupiter', 'jupiter barycenter'),
           ('Saturn', 'saturn barycenter'))

_sky = None


def _skyfield():
    """Timescale and ephemeris, loaded once per process. The ephemeris file is
    downloaded into EPHEMERIS_DIR on first use if it isn't already there (the
    Docker image ships it, so production never downloads)."""
    global _sky
    if _sky is None:
        from skyfield.api import Loader
        load = Loader(EPHEMERIS_DIR, verbose=False)
        _sky = (load.timescale(), load(EPHEMERIS_FILE))
    return _sky


class ChartError(Exception):
    """The chart can't be worked out from these details (e.g. the place
    wasn't found). The message is safe to show to the user."""


# ── Place → coordinates and time zone ─────────────────────────────────────

def geocode(place: str) -> dict:
    """Best match for "City, Region, Country". Open-Meteo searches by name
    only, so the city is searched and the rest of the string picks between
    same-named places (Paris, France vs Paris, Texas)."""
    parts = [p.strip() for p in place.split(',') if p.strip()]
    if not parts:
        raise ChartError('Please add your place of birth.')
    resp = requests.get(GEOCODE_URL, params={'name': parts[0], 'count': 10,
                                             'language': 'en', 'format': 'json'}, timeout=8)
    resp.raise_for_status()
    results = resp.json().get('results') or []
    if not results:
        raise ChartError(f'We could not find "{parts[0]}". Try the nearest city.')

    hints = [p.lower() for p in parts[1:]]

    def score(r):
        fields = ' '.join(str(r.get(k) or '') for k in
                          ('country', 'country_code', 'admin1', 'admin2')).lower()
        return sum(1 for h in hints if h in fields)

    # Ties keep Open-Meteo's own order, which favours larger places.
    best = max(results, key=score)
    if not best.get('timezone'):
        raise ChartError(f'We could not find a time zone for "{place}".')
    label = ', '.join(str(best[k]) for k in ('name', 'admin1', 'country') if best.get(k))
    return {'lat': best['latitude'], 'lon': best['longitude'],
            'tz': best['timezone'], 'label': label}


# ── Astronomy ──────────────────────────────────────────────────────────────

def lahiri_ayanamsa(t) -> float:
    """Lahiri ayanamsa in degrees: 23°51'11" at J2000, precessing ~50.29"/yr."""
    years = (float(t.tt) - 2451545.0) / 365.25
    return 23.853056 + 0.0139694 * years


def _tropical_longitudes(t) -> dict:
    """Apparent geocentric ecliptic-of-date longitudes, plus the mean lunar
    node (Rahu); Ketu is always opposite it."""
    from skyfield.framelib import ecliptic_frame
    ts, eph = _skyfield()
    earth = eph['earth']
    out = {}
    for name, target in _BODIES:
        _, lon, _ = earth.at(t).observe(eph[target]).apparent().frame_latlon(ecliptic_frame)
        out[name] = float(lon.degrees)
    centuries = (float(t.tt) - 2451545.0) / 36525
    out['Rahu'] = (125.04452 - 1934.136261 * centuries) % 360
    out['Ketu'] = (out['Rahu'] + 180) % 360
    return out


def _sidereal_longitudes(t) -> dict:
    ayanamsa = lahiri_ayanamsa(t)
    return {k: (v - ayanamsa) % 360 for k, v in _tropical_longitudes(t).items()}


def _ascendant(t, lat: float, lon: float) -> float:
    """Tropical ascendant from local sidereal time and the obliquity."""
    ramc = math.radians((float(t.gast) * 15 + lon) % 360)
    centuries = (float(t.tt) - 2451545.0) / 36525
    eps = math.radians(23.4392911 - 0.0130042 * centuries)
    phi = math.radians(lat)
    asc = math.degrees(math.atan2(math.cos(ramc),
                                  -(math.sin(ramc) * math.cos(eps) + math.tan(phi) * math.sin(eps))))
    return asc % 360


def _placement(lon: float) -> dict:
    sign = int(lon // 30)
    nak = int(lon // NAKSHATRA_SPAN)
    return {'sign': SIGNS[sign], 'deg': round(lon % 30, 2), 'lon': round(lon, 4),
            'nakshatra': NAKSHATRAS[nak], 'pada': int((lon % NAKSHATRA_SPAN) // (NAKSHATRA_SPAN / 4)) + 1}


def _house(sign_of: str, from_sign: str) -> int:
    return (SIGNS.index(sign_of) - SIGNS.index(from_sign)) % 12 + 1


# ── Vimshottari dashas ─────────────────────────────────────────────────────

def _dasha_timeline(moon_lon: float, born: datetime) -> list:
    """All nine mahadashas from birth, each with its nine antardashas. The
    first period started before birth; how far in is set by how far the Moon
    had moved through its nakshatra."""
    nak = int(moon_lon // NAKSHATRA_SPAN)
    first = nak % 9
    elapsed = (moon_lon % NAKSHATRA_SPAN) / NAKSHATRA_SPAN
    start = born - timedelta(days=DASHA_ORDER[first][1] * elapsed * DASHA_YEAR_DAYS)

    timeline = []
    for i in range(9):
        lord, years = DASHA_ORDER[(first + i) % 9]
        end = start + timedelta(days=years * DASHA_YEAR_DAYS)
        subs, sub_start = [], start
        for j in range(9):
            sub_lord, sub_years = DASHA_ORDER[(first + i + j) % 9]
            sub_end = sub_start + timedelta(days=years * sub_years / 120 * DASHA_YEAR_DAYS)
            subs.append({'lord': sub_lord, 'start': sub_start.date().isoformat(),
                         'end': sub_end.date().isoformat()})
            sub_start = sub_end
        timeline.append({'lord': lord, 'start': start.date().isoformat(),
                         'end': end.date().isoformat(), 'sub': subs})
        start = end
    return timeline


def current_dasha(chart: dict, on: str) -> dict:
    """The mahadasha and antardasha running on `on` (ISO date), plus the next
    two antardashas, for timing. Empty when `on` is outside the timeline."""
    flat = [(md, ad) for md in chart.get('dashas') or [] for ad in md['sub']]
    for i, (md, ad) in enumerate(flat):
        if ad['start'] <= on < ad['end']:
            nxt = [{'maha': m['lord'], 'antar': a['lord'], 'start': a['start']}
                   for m, a in flat[i + 1:i + 3]]
            return {'maha': md['lord'], 'maha_start': md['start'], 'maha_end': md['end'],
                    'antar': ad['lord'], 'antar_start': ad['start'], 'antar_end': ad['end'],
                    'next': nxt}
    return {}


# ── Public API ─────────────────────────────────────────────────────────────

def compute_chart(place: str, birth_date: str, birth_time: str = '') -> dict:
    """The natal chart for these birth details, as JSON-ready data. Without a
    birth time it is cast for local noon and leaves out the ascendant and
    houses from it (they turn over every couple of hours)."""
    geo = geocode(place)
    try:
        day = date.fromisoformat(birth_date)
    except ValueError:
        raise ChartError('Please check your date of birth.')
    time_known = bool(birth_time)
    try:
        hh, mm = (int(x) for x in (birth_time or '12:00').split(':')[:2])
    except ValueError:
        raise ChartError('Please check your time of birth.')
    local = datetime(day.year, day.month, day.day, hh, mm, tzinfo=ZoneInfo(geo['tz']))
    born = local.astimezone(timezone.utc)

    ts, _ = _skyfield()
    t = ts.from_datetime(born)
    lons = _sidereal_longitudes(t)
    # A planet is retrograde when its longitude is falling across the moment.
    before = _sidereal_longitudes(ts.from_datetime(born - timedelta(hours=12)))
    after = _sidereal_longitudes(ts.from_datetime(born + timedelta(hours=12)))

    moon_sign = _placement(lons['Moon'])['sign']
    asc = None
    if time_known:
        asc = _placement((_ascendant(t, geo['lat'], geo['lon']) - lahiri_ayanamsa(t)) % 360)

    planets = {}
    for name, lon in lons.items():
        p = _placement(lon)
        delta = (after[name] - before[name] + 540) % 360 - 180
        p['retro'] = name in ('Rahu', 'Ketu') or (name not in ('Sun', 'Moon') and bool(delta < 0))
        p['house_from_moon'] = _house(p['sign'], moon_sign)
        if asc:
            p['house'] = _house(p['sign'], asc['sign'])
        planets[name] = p

    houses = {}
    if asc:
        first = SIGNS.index(asc['sign'])
        for n in range(1, 13):
            sign = (first + n - 1) % 12
            houses[str(n)] = {'sign': SIGNS[sign], 'lord': SIGN_LORDS[sign],
                              'occupants': [k for k, v in planets.items() if v.get('house') == n]}

    return {
        'v': CHART_VERSION,
        'inputs': {'place_of_birth': place, 'birth_date': birth_date, 'birth_time': birth_time},
        'resolved': {**geo, 'utc': born.strftime('%Y-%m-%dT%H:%M:%SZ'), 'time_known': time_known},
        'ayanamsa': 'lahiri',
        'ascendant': asc,
        'planets': planets,
        'houses': houses,
        'dashas': _dasha_timeline(lons['Moon'], born),
    }


def transits(on: str) -> dict:
    """Sidereal positions at noon UTC on `on` (ISO date). The same for every
    user, and cheap."""
    ts, _ = _skyfield()
    d = date.fromisoformat(on)
    lons = _sidereal_longitudes(ts.utc(d.year, d.month, d.day, 12))
    return {k: _placement(v) for k, v in lons.items()}


def prompt_block(chart: dict, on: str, upcoming: bool = False) -> str:
    """The chart as plain lines for a prompt: natal placements, the dasha
    running on `on`, and that day's transits counted from the ascendant and
    the Moon. `upcoming` adds the full mahadasha list for long-range timing."""
    planets = chart['planets']
    asc = chart.get('ascendant')
    moon_sign = planets['Moon']['sign']
    lines = ['COMPUTED_CHART (sidereal, Lahiri ayanamsa, whole-sign houses)']
    lines.append(f"- Ascendant: {asc['sign']} {asc['deg']}°, {asc['nakshatra']}" if asc
                 else '- Ascendant: unknown (no birth time); read houses from the Moon')
    for name, p in planets.items():
        house = f"house {p['house']}, " if 'house' in p else ''
        lines.append(f"- {name}: {p['sign']} {p['deg']}°, {p['nakshatra']} pada {p['pada']}, "
                     f"{house}house {p['house_from_moon']} from Moon{', retrograde' if p['retro'] else ''}")
    if chart.get('houses'):
        lines.append('- Career houses: ' + '; '.join(
            f"{n}{'nd' if n == '2' else 'th'} {h['sign']} (lord {h['lord']}{', holds ' + ', '.join(h['occupants']) if h['occupants'] else ''})"
            for n, h in chart['houses'].items() if n in ('2', '6', '10', '11')))

    dasha = current_dasha(chart, on)
    if dasha:
        lines.append('')
        lines.append(f'DASHA ON {on}')
        lines.append(f"- Mahadasha: {dasha['maha']} ({dasha['maha_start']} to {dasha['maha_end']})")
        lines.append(f"- Antardasha: {dasha['antar']} ({dasha['antar_start']} to {dasha['antar_end']})")
        for n in dasha['next']:
            lines.append(f"- Then: {n['maha']}/{n['antar']} from {n['start']}")
    if upcoming:
        lines.append('- Mahadasha sequence: ' + ', '.join(
            f"{md['lord']} {md['start'][:4]}-{md['end'][:4]}" for md in chart.get('dashas') or []))

    lines.append('')
    lines.append(f'TRANSITS ON {on}')
    for name, p in transits(on).items():
        from_asc = f"house {_house(p['sign'], asc['sign'])}, " if asc else ''
        lines.append(f"- {name}: {p['sign']} {p['deg']}°, {p['nakshatra']}, "
                     f"{from_asc}house {_house(p['sign'], moon_sign)} from natal Moon")
    return '\n'.join(lines)
