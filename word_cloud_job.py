"""Org word and theme clouds — what people across one organisation bring to Nexa.

Built from every user-side chat message in the org (Nexa's own replies are left
out: they would fill the cloud with coaching vocabulary). Only the counted words
are stored and shown, never a message.

Three things keep the cloud useful and keep it from pointing at anyone:

  * `BLACKLIST` — filler that carries no signal: English stopwords, chat filler
    ("hi", "thanks", "okay"), coaching-chat meta words ("nexa", "advice",
    "help") and common Hinglish fillers. Add to it whenever a word that tells an
    admin nothing turns up in a cloud.
  * the people floor (`_MIN_WORD_USERS`) — a word has to come from several
    different people, so one person's names, projects and turns of phrase
    never surface.
  * the cap (`MAX_CLOUD_WORDS`) — the 30 most-used words that pass both.

Alongside the raw words, the same run builds a theme cloud: an LLM tags a
sample of messages with the topics they're about ("Promotion Readiness",
"Manager Feedback"). Messages go to the model under anonymous m#/P# labels and
it answers with those ids per theme, so the sizes and the people floor are
counted here from its tags, not taken on trust from its numbers. A failed theme
pass leaves the word cloud intact.

One cloud of each is saved per org per day (see `org_word_clouds` in database.init_db).
app.py builds it on a background thread when an admin asks; run this file from
cron to build the day's clouds ahead of time instead.
"""

import os
import re
from collections import Counter, defaultdict

from database import get_org_chat_texts

MAX_CLOUD_WORDS = 30
MAX_CLOUD_THEMES = 20
_MIN_WORD_USERS = 3
_MIN_WORD_LEN = 3
# Newest messages first; enough for a stable picture without loading an org's
# whole history into memory.
_MAX_MESSAGES = 20000

BLACKLIST = frozenset("""
a about above after again against all almost along already also although always am among an
and another any anybody anyone anything anyway anywhere are aren't around as at away back be
became because become becomes been before being below beside besides between beyond both but by
can can't cannot could couldn't did didn't do does doesn't doing don't done down during each
either else enough etc even ever every everybody everyone everything everywhere few for from
further had hadn't has hasn't have haven't having he he'd he'll he's her here here's hers
herself him himself his how how's however i i'd i'll i'm i've if in inside instead into is
isn't it it'd it'll it's its itself just least less let let's like likely may maybe me might
mine more most mostly much must mustn't my myself near nearly neither never next no nobody none
nor not nothing now nowhere of off often on once one only onto or other others otherwise ought
our ours ourselves out outside over own per perhaps please quite rather really same several
shall shan't she she'd she'll she's should shouldn't since so some somebody someone something
sometime sometimes somewhere soon still such than that that's the their theirs them themselves
then there there's therefore these they they'd they'll they're they've this those though through
thus till to together too toward towards under unless until up upon us very via was wasn't we
we'd we'll we're we've well were weren't what what's whatever when whenever where where's
whereas wherever whether which while who who's whoever whole whom whose why why's will with
within without won't would wouldn't yet you you'd you'll you're you've your yours yourself
yourselves

im ive id ill dont doesnt didnt cant couldnt wont wouldnt shouldnt isnt arent wasnt werent
havent hasnt hadnt thats whats theres youre theyre were lets

hi hii hey hello hiya bye goodbye thanks thank thankyou thx ty welcome sorry yes yeah yep yup
yah nope nah okay okk okey alright sure fine cool great nice good awesome lol haha hahaha hmm
hmmm umm uh uhh oh ohh ahh wow pls plz kindly dear sir madam mam maam bro

nexa coach coaching coached chat chatting chatbot bot assistant session sessions conversation
question questions answer answers reply respond response advice advise suggest suggestion
suggestions tip tips help helps helped helpful helping guide guidance explain explanation
example examples tell telling told ask asking asked say saying said share sharing mean means
meant understand understood know knowing knew think thinking thought want wants wanted wanting
need needs needed needing feel feels feeling felt get gets getting got gotten go goes going
gone went come comes coming came make makes making made take takes taking took give gives
giving gave see seeing seen saw look looking looked keep keeping kept try trying tried use
using used put puts call called find found show work able unable sort kind type

thing things stuff something anything everything bit lot lots way ways part kind point case
fact reason idea time times today yesterday tomorrow day days week weeks month months year years
morning evening night now currently recently lately right new old first last next
little big small large long short high low many much more most less least few
one two three four five six seven eight nine ten hundred thousand first second third

also again always actually basically literally definitely probably maybe just only really very
quite pretty totally completely exactly simply especially usually generally honestly seriously

hai hain ho hoon hu tha thi the ka ki ke ko se me mein mai main aur ya bhi nahi nahin na kya
kyu kyun kaise kaisa kaun koi kuch bahut bohot accha acha theek thik haan han ji yaar matlab
abhi phir fir toh wala wali wale raha rahi rahe karna karni karu karo kar kiya kare karte karta hota hoti hote baat
""".split())

_URL = re.compile(r'(https?://|www\.)\S+', re.I)
_EMAIL = re.compile(r'\S+@\S+')
_WORD = re.compile(r"[a-z][a-z']{%d,}" % (_MIN_WORD_LEN - 1))


def _tokenise(text: str) -> list:
    """Words worth counting: letters with internal apostrophes, lowercased.

    URLs, emails, numbers and @handles go first — they identify far more
    readily than they inform.
    """
    text = _EMAIL.sub(' ', _URL.sub(' ', text or '')).lower()
    words = []
    for w in _WORD.findall(text):
        w = w.strip("'")
        if w.endswith("'s"):        # "manager's" counts as "manager"
            w = w[:-2]
        if len(w) >= _MIN_WORD_LEN and w not in BLACKLIST:
            words.append(w)
    return words


def _fold_plurals(counts: Counter) -> dict:
    """Map 'deadlines' onto 'deadline' when both were said, so a theme isn't
    split across two smaller words. Only folds when the singular is itself in
    the counts — that skips 'process', 'business' and the like."""
    fold = {}
    for w in counts:
        if len(w) > 4 and w.endswith('s') and not w.endswith('ss') and w[:-1] in counts:
            fold[w] = w[:-1]
    return fold


def build_word_cloud(rows: list) -> dict:
    """{words: [{text, weight, people}], message_count, user_count} from
    (user_id, content) rows.

    `weight` is total mentions and sizes the word; `people` is how many
    different people used it, which is what the floor is applied to.
    """
    total = Counter()
    people = {}
    users = set()
    for user_id, content in rows:
        tokens = _tokenise(content)
        if not tokens:
            continue
        users.add(user_id)
        total.update(tokens)
        for w in set(tokens):
            people.setdefault(w, set()).add(user_id)

    for plural, singular in _fold_plurals(total).items():
        total[singular] += total.pop(plural)
        people[singular] |= people.pop(plural)

    words = [{'text': w, 'weight': n, 'people': len(people[w])}
             for w, n in total.items() if len(people[w]) >= _MIN_WORD_USERS]
    words.sort(key=lambda w: (-w['weight'], w['text']))
    return {
        'words': words[:MAX_CLOUD_WORDS],
        'message_count': len(rows),
        'user_count': len(users),
    }


# What the theme pass sends the model: up to this many messages, each cut to a
# few hundred characters, picked round-robin across people so one heavy user
# can't decide the themes.
_THEME_SAMPLE_MESSAGES = 400
_THEME_MESSAGE_CHARS = 400
_THEME_PROMPT_CHARS = 60000

_THEME_PROMPT = """You are finding the themes employees at one organisation raise with Nexa, their AI work coach. An HR leader will see the result as a theme cloud.

Each line below is one message, labelled [m<message id>|P<person id>]. The labels are anonymous.

Rules — every one is mandatory:
- A theme is a workplace or career topic someone could act on, named in 1-4 words, Title Case (e.g. "Promotion Readiness", "Manager Feedback", "Workload Pressure", "Work-Life Balance").
- NEVER name a person, team, project, client, place or anything else that could point at one individual.
- Skip greetings, small talk, and questions about Nexa itself.
- Tag every message that is genuinely about a theme with that theme's id; a message may carry more than one theme.
- Only keep a theme that messages from at least THREE different P ids are about.
- Give at most {max_themes} themes, most talked-about first.

Reply with ONLY this JSON, no prose around it:
{{"themes": [{{"theme": "<1-4 words>", "messages": ["m12", "m40"]}}]}}

The messages:
---
{messages}
---"""


def _theme_sample(rows: list) -> list:
    """(message id, person id, text) — round-robin across people, newest first
    within each, trimmed to the prompt budget."""
    by_user = defaultdict(list)
    for user_id, content in rows:
        text = ' '.join(_EMAIL.sub(' ', _URL.sub(' ', content or '')).split())
        if len(text) >= 15:
            by_user[user_id].append(text[:_THEME_MESSAGE_CHARS])
    people = {u: i + 1 for i, u in enumerate(by_user)}
    queues = [(people[u], msgs) for u, msgs in by_user.items()]
    sample, budget, depth = [], _THEME_PROMPT_CHARS, 0
    while len(sample) < _THEME_SAMPLE_MESSAGES and any(depth < len(m) for _, m in queues):
        for pid, msgs in queues:
            if depth < len(msgs) and len(sample) < _THEME_SAMPLE_MESSAGES:
                budget -= len(msgs[depth]) + 16
                if budget < 0:
                    return sample
                sample.append((len(sample) + 1, pid, msgs[depth]))
        depth += 1
    return sample


def build_theme_cloud(rows: list) -> list | None:
    """[{text, weight, people}] — weight is how many sampled messages carry the
    theme. None when there's no model to ask or the call fails; [] when
    nothing clears the floor."""
    from sentiment_job import _build_client, _call_llm, _extract_json

    sample = _theme_sample(rows)
    if len({pid for _, pid, _ in sample}) < _MIN_WORD_USERS:
        return []
    ai_provider = os.environ.get('AI_PROVIDER', 'claude').lower()
    api_key = os.environ.get('CLAUDE_API_KEY' if ai_provider == 'claude' else 'OPENAI_API_KEY')
    if not api_key:
        print("Theme cloud: no API key configured.")
        return None
    ai_model = os.environ.get('AI_MODEL', 'claude-sonnet-4-6' if ai_provider == 'claude' else 'gpt-4o')

    person_of = {f'm{mid}': pid for mid, pid, _ in sample}
    prompt = _THEME_PROMPT.format(
        max_themes=MAX_CLOUD_THEMES,
        messages='\n'.join(f'[m{mid}|P{pid}] {text}' for mid, pid, text in sample),
    )
    try:
        raw = _call_llm(prompt, _build_client(ai_provider, api_key), ai_provider, ai_model,
                        max_tokens=4000)
        result = _extract_json(raw)
    except Exception as e:
        print(f"Theme cloud failed: {e}")
        return None

    themes = {}
    for t in result.get('themes') or []:
        if not isinstance(t, dict):
            continue
        name = ' '.join(str(t.get('theme') or '').split())[:40]
        ids = {str(m) for m in (t.get('messages') or [])} & person_of.keys()
        if not name or name.lower() in themes:
            continue
        people = {person_of[m] for m in ids}
        if len(people) >= _MIN_WORD_USERS:
            themes[name.lower()] = {'text': name, 'weight': len(ids), 'people': len(people)}
    out = sorted(themes.values(), key=lambda t: (-t['weight'], t['text']))
    return out[:MAX_CLOUD_THEMES]


def build_org_word_cloud(org_slug: str) -> dict:
    """Both clouds for one org. `themes` is None when the theme pass couldn't
    run — the words still stand."""
    rows = get_org_chat_texts(org_slug, limit=_MAX_MESSAGES)
    cloud = build_word_cloud(rows)
    cloud['themes'] = build_theme_cloud(rows)
    return cloud


if __name__ == '__main__':
    import sys
    from datetime import date
    from database import get_all_org_slugs, claim_word_cloud_run, finish_word_cloud_run

    # Pre-builds today's cloud so the first admin of the day doesn't wait for it.
    # A claim that fails means a run for that org is already going.
    today = date.today()
    for slug in sys.argv[1:] or get_all_org_slugs():
        if not claim_word_cloud_run(slug, today):
            print(f"Skipping {slug} — a run is already in progress.")
            continue
        cloud = build_org_word_cloud(slug)
        finish_word_cloud_run(slug, today, cloud)
        print(f"{slug}: {len(cloud['words'])} words, {len(cloud['themes'] or [])} themes "
              f"from {cloud['message_count']} messages")
