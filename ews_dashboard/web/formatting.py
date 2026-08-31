"""Template filters and the words a state is shown as.

Every filter has a defined answer for None, because a missing number and a zero mean different
things throughout this data: no classifiable builds is not a perfect score, and a queue that never
asked about flakiness is not a queue that asked and convicted nothing.
"""

from __future__ import annotations

import datetime
from typing import Optional

from flask import Flask

from ews_dashboard.analysis import false_positive

MISSING = '—'

# Not a stored bucket: a build that showed its author no failures is fully classified and belongs in
# no denominator, which reads nothing like a build no refresh has reached.
NO_SURFACED = 'no_surfaced'

# Not a stored bucket either: no refresh has reached the build, which is what a reader filtering for
# a gap in the data is looking for.
UNCLASSIFIED = 'unclassified'

# The stored name is what a page links and anchors on; these are what it reads as. Both a colour and
# a word, because colour alone is unreadable to anyone who cannot separate red from green.
BUCKET_WORDS = {
    false_positive.CLEAN: 'real failure',
    false_positive.PARTIAL_FP: 'partly noise',
    false_positive.FALSE_RED: 'all noise',
    false_positive.UNDETERMINED: 'undetermined',
    NO_SURFACED: 'nothing shown',
}

VERDICT_WORDS = {
    false_positive.REAL: 'real',
    false_positive.PRE_EXISTING: 'noise',
    false_positive.NO_HISTORY: 'no history',
    false_positive.UNQUERIED: 'not looked up',
}

# A bucket or verdict that reads as blame noise, which is the state this dashboard exists to surface.
BLAMES_NOISE = frozenset((false_positive.PARTIAL_FP, false_positive.FALSE_RED,
                          false_positive.PRE_EXISTING))

# Every state the builds pane can show a build in, in the order it reads best as a list of choices.
STATE_CHOICES = (false_positive.CLEAN, false_positive.PARTIAL_FP, false_positive.FALSE_RED,
                 false_positive.UNDETERMINED, NO_SURFACED, UNCLASSIFIED)


def state(name: Optional[str]) -> str:
    """One bucket or verdict as a word. An unclassified build has no state name at all."""
    if name is None:
        return UNCLASSIFIED
    return BUCKET_WORDS.get(name) or VERDICT_WORDS.get(name) or name


def state_class(name: Optional[str]) -> str:
    if name is None:
        return 'state-unknown'
    return f'state-{name}'


def percent(value: Optional[float]) -> str:
    if value is None:
        return MISSING
    return f'{value:g}%'


def count(value: Optional[int]) -> str:
    if value is None:
        return MISSING
    return f'{value:,}'


def moment(value: Optional[int]) -> str:
    if value is None:
        return MISSING
    at = datetime.datetime.fromtimestamp(value, datetime.timezone.utc)
    return at.strftime('%Y-%m-%d %H:%M UTC')


def age(seconds: Optional[int]) -> str:
    if seconds is None:
        return 'never'
    if seconds < 90:
        return 'moments ago'
    for unit, length in (('day', 86400), ('hour', 3600), ('minute', 60)):
        if seconds >= length:
            amount = seconds // length
            return f'{amount} {unit}{"s" if amount != 1 else ""} ago'
    return 'moments ago'


def duration(seconds: Optional[int]) -> str:
    if seconds is None:
        return MISSING
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f'{minutes}m {remainder:02d}s'
    hours, minutes = divmod(minutes, 60)
    return f'{hours}h {minutes:02d}m'


def register(app: Flask) -> None:
    for name, function in (('percent', percent), ('count', count), ('moment', moment), ('age', age),
                           ('duration', duration), ('state', state), ('state_class', state_class)):
        app.jinja_env.filters[name] = function
    app.jinja_env.globals['blames_noise'] = BLAMES_NOISE
