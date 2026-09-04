"""Links out to the sites that already own this data.

The organizing rule of this dashboard is that it renders only the join between a conviction and the
rule behind it. Test history belongs to results.webkit.org and build detail belongs to EWS, so both
are linked, never re-rendered.
"""

from __future__ import annotations

import urllib.parse
from typing import Optional

from ews_dashboard import config, results

WEBKIT_PULL_REQUEST_URL = 'https://github.com/WebKit/WebKit/pull'


def _test_parameters(configuration: results.Configuration, test_name: str) -> dict:
    return {'suite': configuration.suite, 'test': test_name}


def test_history(configuration: results.Configuration, test_name: str) -> str:
    """Where a test started flaking, on results.webkit.org's own timeline.

    `test_investigation` builds the same `RESULTS_URL` query, just unfiltered by configuration.
    """
    parameters = _test_parameters(configuration, test_name)
    parameters.update(configuration.query_parameters())
    return f'{config.RESULTS_URL}/?{urllib.parse.urlencode(parameters)}'


def test_investigation(configuration: results.Configuration, test_name: str) -> str:
    """The same timeline unfiltered by configuration, so every configuration of the test shows."""
    query = urllib.parse.urlencode(_test_parameters(configuration, test_name))
    return f'{config.RESULTS_URL}/?{query}'


def build(builder_id: int, build_number: int) -> str:
    return f'{config.BUILDBOT_URL}/#/builders/{builder_id}/builds/{build_number}'


def pull_request(pr_id: Optional[int]) -> Optional[str]:
    if pr_id is None:
        return None
    return f'{WEBKIT_PULL_REQUEST_URL}/{pr_id}'
