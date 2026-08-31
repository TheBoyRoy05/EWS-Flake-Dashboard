"""Values shared by ingest, analysis and the web layer.

Nothing here reaches the network or the filesystem at import time.
"""

from __future__ import annotations

import datetime
import os
from dataclasses import dataclass
from typing import Optional

BUILDBOT_URL = 'https://ews-build.webkit.org'
RESULTS_URL = 'https://results.webkit.org'

DATABASE_PATH_VARIABLE = 'EWS_DASHBOARD_DATABASE'
DEFAULT_DATABASE_NAME = 'ews-dashboard.db'

CHECKOUT_PATH_VARIABLE = 'EWS_DASHBOARD_CHECKOUT'

# A test passing this often or less on main already fails without the change, so a failure here is
# not the author's doing.
PRE_EXISTING_THRESHOLD_PCT = 80

# How long after a change lands main is watched for the test a build called flaky, and how long
# before it lands is read as the baseline. Three days matches the window EWS's own rules use, so a
# test convicted for flaking over three days is checked over the same span.
ESCAPE_WINDOW_DAYS = 3

# What share of a test's post-landing runs on main must fail unexpectedly before the failure is a
# regression rather than the flakiness the build was told it was. Below this the conviction is
# corroborated: the test really does fail some of the time on main.
ESCAPE_FAILURE_PCT = 50

# Above this many author-visible failures the build is a crash storm, not a set of test results;
# classifying it test-by-test would cost hundreds of lookups to describe a broken checkout.
MAX_CLASSIFIABLE_SURFACED_TESTS = 60

CLEAN_TREE = 'CleanTree'
DIRTY_TREE = 'DirtyTree'
BETWEEN_BUILDS = 'BetweenBuilds'

# Display order, not alphabetical: CleanTree is the only rule that convicts on a single row.
FLAKINESS_RULES = (CLEAN_TREE, DIRTY_TREE, BETWEEN_BUILDS)

# Thresholds are ResultsDatabase's in results_db.py: DirtyTree wants 2 pull requests and 1 author.
# BetweenBuilds wants 3 pull requests and 2 authors. Both windows are FLAKY_WINDOW_SECONDS, 3 days.
RULE_DESCRIPTIONS = {
    CLEAN_TREE: 'Recorded flaky with no change applied, so the change cannot be the cause.',
    DIRTY_TREE: 'Recorded flaky with a change applied, in 2 or more pull requests in 3 days.',
    BETWEEN_BUILDS: 'Recorded failing across 3 or more pull requests and 2 authors in 3 days.',
}


@dataclass(frozen=True)
class Deployment:
    """A production change to EWS whose effect the trend chart has to be read against."""

    at: datetime.datetime
    label: str
    detail: str


DEPLOYMENTS = (
    Deployment(
        at=datetime.datetime(2026, 8, 14, tzinfo=datetime.timezone.utc),
        label='write',
        detail='EWS begins recording flaky and failed tests to results.webkit.org.',
    ),
    Deployment(
        at=datetime.datetime(2026, 8, 25, 11, 18, 23, tzinfo=datetime.timezone.utc),
        label='read',
        detail='First build carrying a results-db flakiness property, so the read path is live.',
    ),
    Deployment(
        at=datetime.datetime(2026, 8, 31, 10, 52, 37, tzinfo=datetime.timezone.utc),
        label='act',
        detail='First build to ignore a flaky failure rather than log that it would have, so '
               'suppression is live. The master picked the change up 58 hours after it landed.',
    ),
)


def database_path() -> str:
    """Where the sqlite database lives, overridable for tests and for a hosted refresh."""
    from_environment = os.environ.get(DATABASE_PATH_VARIABLE)
    if from_environment:
        return from_environment
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        DEFAULT_DATABASE_NAME)


def checkout_path() -> Optional[str]:
    """A WebKit checkout to read landings from, or None when the refresh has not been given one.

    There is no default. A path guessed wrong reads as thousands of pull requests that never landed,
    which is indistinguishable on a page from thousands that really did not.
    """
    return os.environ.get(CHECKOUT_PATH_VARIABLE) or None
