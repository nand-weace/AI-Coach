"""Common translation for generated content.

`translate_content` translates the human-readable strings inside any
JSON-shaped value (dict / list / str) into a target language and returns a copy
with the same shape — keys, numbers, booleans, dates, URLs and any field named
in `skip_keys` come back untouched, so a translated report renders through the
same code as the original.

Used by /api/translate (My Insights), which saves the result over the original
so each report is translated once per language switch. Anything else that needs
a translated copy of generated content should go through it too.
"""
import json
import logging
import re

from languages import prompt_name

logger = logging.getLogger(__name__)

DEFAULT_LANGUAGE = 'English'

# Keys whose values are never prose: identifiers, enums the UI switches on,
# dates, links. Applied at every depth.
ALWAYS_SKIP = frozenset({
    'id', 'key', 'code', 'slug', 'type', 'status', 'url', 'language',
    'calculated_at', 'created_at', 'date', 'for_date', 'first_seen', 'last_seen',
    'period_from', 'period_to', 'momentum', 'sentiment', 'trajectory',
    'dimension', 'evidence_strength', 'verified', 'web_searched',
})


# Strings per model call — keeps each reply well inside max_tokens.
_BATCH_STRINGS = 60
_BATCH_CHARS = 8000

_URL_RE = re.compile(r'^(https?://|www\.)\S+$', re.I)
_ISO_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}([ T][\d:.+\-Z]*)?$')

_SYSTEM = """You translate text for a coaching app. You are given a JSON array of strings. \
Translate each string into {target}, keeping the meaning, tone and second-person voice. \
Keep names, numbers, emoji and any placeholders as they are. A string that is a direct \
quote of the user's own words, a proper name, or a book/article title stays unchanged. \
Reply with ONLY a JSON object {{"t": [...]}} holding exactly as many strings as you were \
given, in the same order."""


class TranslationError(Exception):
    """The model did not return a usable translation."""


def _is_translatable(text: str) -> bool:
    s = text.strip()
    if not s or not any(ch.isalpha() for ch in s):
        return False
    return not (_URL_RE.match(s) or _ISO_DATE_RE.match(s))


def _collect(value, skip_keys, out: list):
    """Gather every translatable string in `value`, in a stable order."""
    if isinstance(value, str):
        if _is_translatable(value):
            out.append(value)
    elif isinstance(value, list):
        for v in value:
            _collect(v, skip_keys, out)
    elif isinstance(value, dict):
        for k, v in value.items():
            if k not in skip_keys:
                _collect(v, skip_keys, out)


def _apply(value, skip_keys, table: dict):
    if isinstance(value, str):
        return table.get(value, value)
    if isinstance(value, list):
        return [_apply(v, skip_keys, table) for v in value]
    if isinstance(value, dict):
        return {k: (v if k in skip_keys else _apply(v, skip_keys, table))
                for k, v in value.items()}
    return value


def _batches(strings: list):
    batch, size = [], 0
    for s in strings:
        if batch and (len(batch) >= _BATCH_STRINGS or size + len(s) > _BATCH_CHARS):
            yield batch
            batch, size = [], 0
        batch.append(s)
        size += len(s)
    if batch:
        yield batch


def _translate_batch(strings: list, target: str, cfg) -> list:
    from sentiment_job import _call_llm, _extract_json
    client, ai_provider, ai_model = cfg
    raw = _call_llm(
        json.dumps(strings, ensure_ascii=False),
        client, ai_provider, ai_model,
        # Non-Latin scripts cost several tokens a character; leave room.
        max_tokens=min(8192, 1024 + sum(len(s) for s in strings) * 3),
        system=_SYSTEM.format(target=prompt_name(target)),
    )
    out = _extract_json(raw).get('t')
    if not isinstance(out, list) or len(out) != len(strings):
        raise TranslationError('Translation came back in the wrong shape.')
    return [str(t) if isinstance(t, (str, int, float)) and str(t).strip() else src
            for t, src in zip(out, strings)]


def translate_content(content, target_language: str, source_language: str = None,
                      skip_keys=()):
    """Return a copy of `content` with its prose translated into `target_language`.

    `content` may be a str, list or dict, nested to any depth. Keys in
    ALWAYS_SKIP and `skip_keys` keep their values as-is. Identical strings are
    translated once. A no-op when source and target are the same language.
    Raises TranslationError if the model is unavailable or answers badly.
    """
    target = target_language or DEFAULT_LANGUAGE
    if (source_language or DEFAULT_LANGUAGE) == target:
        return content

    skip = ALWAYS_SKIP | frozenset(skip_keys)
    strings = []
    _collect(content, skip, strings)
    unique = list(dict.fromkeys(strings))
    if not unique:
        return content

    from sentiment_job import _llm_config
    cfg = _llm_config()
    if not cfg:
        raise TranslationError('No AI provider configured.')

    table = {}
    try:
        for batch in _batches(unique):
            table.update(zip(batch, _translate_batch(batch, target, cfg)))
    except TranslationError:
        raise
    except Exception as e:
        logger.warning('[translate] %s -> %s failed: %s', source_language, target, e)
        raise TranslationError('Translation failed.') from e
    return _apply(content, skip, table)
