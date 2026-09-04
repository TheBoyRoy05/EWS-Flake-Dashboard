"""What each link out promises, since a wrong query string fails only in the browser.

results.webkit.org's /investigate is a suite-level page and rejects a `test` parameter outright, so
widening a link to every configuration means dropping the configuration keys from the root timeline,
not switching endpoints.
"""

from __future__ import annotations

import unittest
from urllib.parse import parse_qs, urlsplit

from ews_dashboard import results
from ews_dashboard.web import links

CONFIGURATION = results.Configuration(suite='layout-tests', platform='mac', style='release',
                                      flavor='wk2')
TEST_NAME = 'fast/flexbox/wrap-balance.html'


class TestHistoryTest(unittest.TestCase):
    def test_carries_the_whole_configuration(self) -> None:
        parameters = parse_qs(urlsplit(links.test_history(CONFIGURATION, TEST_NAME)).query)
        self.assertEqual(parameters, {
            'suite': ['layout-tests'],
            'test': [TEST_NAME],
            'platform': ['mac'],
            'style': ['release'],
            'flavor': ['wk2'],
        })

    def test_omits_an_absent_flavor(self) -> None:
        configuration = results.Configuration(suite='layout-tests', platform='mac', style='release')
        parameters = parse_qs(urlsplit(links.test_history(configuration, TEST_NAME)).query)
        self.assertNotIn('flavor', parameters)


class TestInvestigationTest(unittest.TestCase):
    def test_is_the_root_timeline_not_the_suite_page(self) -> None:
        url = links.test_investigation(CONFIGURATION, TEST_NAME)
        self.assertNotIn('/investigate', url)
        self.assertEqual(urlsplit(url).path, '/')

    def test_names_the_test_and_its_suite(self) -> None:
        parameters = parse_qs(urlsplit(links.test_investigation(CONFIGURATION, TEST_NAME)).query)
        self.assertEqual(parameters['suite'], ['layout-tests'])
        self.assertEqual(parameters['test'], [TEST_NAME])

    def test_drops_every_configuration_key(self) -> None:
        parameters = parse_qs(urlsplit(links.test_investigation(CONFIGURATION, TEST_NAME)).query)
        for key in ('platform', 'style', 'flavor'):
            self.assertNotIn(key, parameters)


if __name__ == '__main__':
    unittest.main()
