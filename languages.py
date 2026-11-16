"""The languages Nexa supports — one list for the server.

Code -> (label shown in the picker, name used in prompts). Keep in sync with
LANGUAGES in static/header.js and the dictionaries in static/i18n/.
"""

SUPPORTED_LANGUAGES = {
    'English': ('English', 'English'),
    'Hindi':   ('हिन्दी (Hindi)', 'Hindi'),
    'Arabic':  ('العربية (Arabic)', 'Arabic'),
    'Spanish': ('Español (Spanish)', 'Spanish'),
    'Tamil':   ('தமிழ் (Tamil)', 'Tamil'),
    'French':  ('Français (French)', 'French'),
    'German':  ('Deutsch (German)', 'German'),
}
DEFAULT_LANGUAGE = 'English'


def normalize_language(code) -> str:
    """A supported language code; anything else (unset, or a language that has
    since been dropped from the list) falls back to English."""
    return code if code in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def prompt_name(code) -> str:
    return SUPPORTED_LANGUAGES[normalize_language(code)][1]
