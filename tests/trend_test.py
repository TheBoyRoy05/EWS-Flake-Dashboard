"""Daily buckets are UTC, and the rolling value sums counts rather than averaging percentages."""

from __future__ import annotations

import datetime

from ews_dashboard import config
from ews_dashboard.analysis import false_positive, trend
from tests import fixtures

LAST_DAY = datetime.date(2026, 8, 20)
RELIABLE = 99.5
UNRELIABLE = 40.0


def _noon(day: datetime.date) -> int:
    return trend.day_bounds(day)[0] + 43200


class TestDaily(fixtures.DatabaseTest):
    def _points(self, days: int = 3) -> list:
        history = fixtures.StubHistory({'fast/pre.html': UNRELIABLE, 'fast/real.html': RELIABLE})
        return trend.daily(
            self.connection, false_positive.live_classifier(self.connection, history),
            LAST_DAY, days=days,
        )

    def _store_blamed_build(self, number: int, day: datetime.date, blamed: bool = True) -> None:
        test_name = 'fast/pre.html' if blamed else 'fast/real.html'
        self.store_build(number, first=[test_name], second=[test_name], clean=[],
                         started_at=_noon(day))

    def test_one_point_per_day_oldest_first(self) -> None:
        points = self._points(days=3)
        self.assertEqual([point.day for point in points],
                         [LAST_DAY - datetime.timedelta(days=2),
                          LAST_DAY - datetime.timedelta(days=1), LAST_DAY])

    def test_a_day_with_no_builds_has_no_rate_rather_than_a_zero(self) -> None:
        self.assertIsNone(self._points()[0].daily_pct)

    def test_a_days_rate_covers_only_that_day(self) -> None:
        self._store_blamed_build(1, LAST_DAY)
        self._store_blamed_build(2, LAST_DAY - datetime.timedelta(days=1), blamed=False)
        points = self._points(days=2)
        self.assertEqual(points[0].daily_pct, 0.0)
        self.assertEqual(points[1].daily_pct, 100.0)

    def test_a_build_late_in_a_utc_day_belongs_to_that_day(self) -> None:
        last_second = trend.day_bounds(LAST_DAY)[1] - 1
        self.store_build(1, first=['fast/pre.html'], second=['fast/pre.html'], clean=[],
                         started_at=last_second)
        self.assertEqual(self._points(days=1)[0].daily_pct, 100.0)

    def test_the_rolling_value_weights_a_busy_day_more_than_a_quiet_one(self) -> None:
        quiet_day = LAST_DAY - datetime.timedelta(days=1)
        self._store_blamed_build(1, quiet_day)
        for number in range(2, 11):
            self._store_blamed_build(number, LAST_DAY, blamed=False)
        points = self._points(days=2)
        self.assertEqual(points[0].daily_pct, 100.0)
        self.assertEqual(points[1].daily_pct, 0.0)
        # 1 blamed build out of 10 classifiable across the window, not the mean of 100% and 0%.
        self.assertEqual(points[1].rolling_pct, 10.0)

    def test_the_rolling_value_reaches_back_beyond_the_first_returned_day(self) -> None:
        self._store_blamed_build(1, LAST_DAY - datetime.timedelta(days=3))
        points = self._points(days=2)
        self.assertIsNone(points[0].daily_pct)
        self.assertEqual(points[0].rolling_pct, 100.0)


class TestDeploymentsWithin(fixtures.DatabaseTest):
    def _points(self, days: list) -> list:
        return [trend.Point(day, false_positive.Counts(), None) for day in days]

    def test_a_deployment_inside_the_range_is_reported(self) -> None:
        deployment = config.DEPLOYMENTS[0]
        day = deployment.at.date()
        found = trend.deployments_within(self._points([day - datetime.timedelta(days=1), day]))
        self.assertEqual(found, [deployment])

    def test_a_deployment_outside_the_range_is_not(self) -> None:
        day = config.DEPLOYMENTS[0].at.date() - datetime.timedelta(days=30)
        self.assertEqual(trend.deployments_within(self._points([day])), [])

    def test_an_empty_series_has_no_deployments_to_mark(self) -> None:
        self.assertEqual(trend.deployments_within([]), [])
