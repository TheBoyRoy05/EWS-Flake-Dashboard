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
    parameters = {
        'suite': configuration.suite,
        'test': test_name,
        'platform': configuration.platform,
        'style': configuration.style,
    }
    if configuration.flavor:
        parameters['flavor'] = configuration.flavor
    return parameters


def test_history(configuration: results.Configuration, test_name: str) -> str:
    """Where a test started flaking, on results.webkit.org's own timeline.

    This is the shape investigate.html builds for the same purpose, so it stays correct as that
    page evolves; the configuration comes out of the query string there too.
    """
    query = urllib.parse.urlencode(_test_parameters(configuration, test_name))
    return f'{config.RESULTS_URL}/?{query}'


def test_investigation(configuration: results.Configuration, test_name: str) -> str:
    """The same test across every configuration, for deciding whether a flake is platform-specific."""
    query = urllib.parse.urlencode(_test_parameters(configuration, test_name))
    return f'{config.RESULTS_URL}/investigate?{query}'


def build(builder_id: int, build_number: int) -> str:
    return f'{config.BUILDBOT_URL}/#/builders/{builder_id}/builds/{build_number}'


def pull_request(pr_id: Optional[int]) -> Optional[str]:
    if pr_id is None:
        return None
    return f'{WEBKIT_PULL_REQUEST_URL}/{pr_id}'
