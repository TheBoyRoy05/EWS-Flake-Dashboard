"""Word budgets for the blocks a reader scans, measured on the rendered page.

FIX A FAILURE HERE BY CUTTING COPY, NEVER BY RAISING THE CEILING. A ceiling moved to make the suite
green is a budget that has stopped measuring anything, and the block it guards is free to grow again.
Raising one is a deliberate edit with a reason written beside it, not a way past a red test.

Prose grows one clause at a time and every clause looks harmless, which is why this is a test and not
a note in a style guide. A reader has a small budget of attention, spends it scanning rather than
reading, and an explanation twice as long is usually less informative because it pushes the
load-bearing figure out of view. Three things are measured:

* every block in `BUDGETS` against its ceiling;
* the widest single cell of the DRILLDOWN's reason column, which is where 4,359 words of English used
  to live, and the whole of a row in either table — a LISTING row must stay a line of figures rather
  than growing a description of the group it stands for, and a DRILLDOWN row is where the reason phrase
  that replaced those 4,359 words sits; and
* repetition — a listing row may print no count pair at all (no pooling of the three is honest), and a
  drilldown row prints each of its three exactly once. The column that grew to 4,359 words fitted no
  ceiling because no ceiling existed, and most of what it said was a figure printed again in the same
  row.

Counted on the RENDERED page: tags stripped, entities resolved, whitespace collapsed. The source
carries comments, class names and URLs nobody reads, and it is the reader's attention being budgeted.
"""

from __future__ import annotations

import html
import re
import time
from typing import NamedTuple, Optional

from ews_dashboard import config
from ews_dashboard.analysis import escapes
from tests import fixtures
from tests.web_test import WebTest

ESCAPES = '/escapes'

TAGS = re.compile(r'<[^>]+>')
SPACE = re.compile(r'\s+')

# The listing and drilldown tables, located by their own opening tags. The drilldown's carries an
# `aria-labelledby` after the class, so it is matched on the prefix.
LISTING_TABLE = '<table class="table full-width convictions">'
DRILLDOWN_TABLE = '<table class="table full-width escape-convictions"'

# How many rendered words a LISTING row may hold, which is a ceiling on the row itself rather than on
# one cell. A row is Test, Rule, Queues, Count, Landed and — in the merged escape bucket only — Rate
# increase and Current damage: a test path and a rule name, four figures and a date. The widest row
# measured 10 words, and the cap exists because a grouped row is exactly where a description of the
# group would be tempting — the honest ones are the column headers' own title attributes, which cost no
# rendered words.
LISTING_ROW_WORDS = 12

# The same ceiling for a DRILLDOWN row, which holds one conviction: a queue name, a date, the count
# pairs or a reason phrase, and three link names. The widest row measured 11 words, and a row in a
# category main answered nothing about spends `escapes.REASON_WORDS` of its own on the reason.
DRILLDOWN_ROW_WORDS = 14


class Budget(NamedTuple):
    """One block of a page, and the most rendered words it may hold.

    `opening` is the exact opening tag to look for and `tag` its element name, so a block is cut at
    its own closing tag rather than at the first one that follows it. A budget is one line to add.
    """

    name: str
    path: str
    opening: str
    tag: str
    ceiling: int


BUDGETS = (
    # The definitions and the caveats, folded into one collapsed block from two that together ran to
    # 188 words. 146 rendered when this was written.
    Budget('escapes legend', ESCAPES, '<details class="section legend-pane">', 'details', 150),
    # The two headline figures and their labels.
    Budget('escapes headline figures', ESCAPES, '<div class="strip-figures">', 'div', 50),
    # The splits under the escape bucket, which are labels beside counts rather than prose. 53.
    Budget('escapes significance split', ESCAPES, '<div class="subcategories">', 'div', 60),
)

# The reason column's own ceiling is `escapes.REASON_WORDS`: a category main answered nothing about
# gets a phrase because an em dash alone does not say why, and the phrase is capped so the column
# cannot grow back into the prose it replaced. The escape bucket's cell is a count pair, well inside
# it.


def words(fragment: str) -> list:
    return [word for word in SPACE.sub(' ', html.unescape(TAGS.sub(' ', fragment))).split(' ')
            if word]


def block(page: str, opening: str, tag: str) -> str:
    """The whole of one element, opening tag to matching closing tag, nesting counted.

    Raises rather than returning nothing when the opening tag is absent: a locator that has gone stale
    must fail the budget it belongs to, not quietly measure an empty string and pass.
    """
    start = page.index(opening)
    opens, closes = f'<{tag}', f'</{tag}>'
    depth, index = 0, start
    while True:
        opened = page.find(opens, index)
        closed = page.find(closes, index)
        if closed == -1:
            raise AssertionError(f'{opening} is never closed')
        if opened != -1 and opened < closed:
            depth += 1
            index = opened + len(opens)
            continue
        depth -= 1
        index = closed + len(closes)
        if depth == 0:
            return page[start:index]


def _table_body(page: str, opening: str) -> str:
    """The `<tbody>` of the table that opening tag starts.

    Raises rather than returning nothing when the table is absent, for the reason `block` does: a row
    helper that quietly finds no rows turns every ceiling taken over those rows green without measuring
    a single one.
    """
    table = page.index(opening)
    body = page.index('<tbody>', table) + len('<tbody>')
    return page[body:page.index('</tbody>', body)]


def _rows(body: str) -> list:
    return [f'<tr{fragment.split("</tr>")[0]}</tr>' for fragment in body.split('<tr')[1:]]


def rows(page: str) -> list:
    """Each row of the LISTING, as its own HTML. Selected out of its own table's `<tbody>` rather than
    by the `class` attribute a row carries, which only the open row has one of."""
    return _rows(_table_body(page, LISTING_TABLE))


def drilldown_rows(page: str) -> list:
    """Each conviction row of the open drilldown, as its own HTML."""
    return _rows(_table_body(page, DRILLDOWN_TABLE))


def cells(row: str) -> list:
    return re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)


def reason_cell(row: str) -> str:
    """The DRILLDOWN cell right after Landed: the baseline pair in the merged escape bucket, a reason
    phrase everywhere else. Found by its neighbour, not by position, because the Queue column before
    Landed only renders when the pane spans more than one queue."""
    after_landed = re.search(r'<td class="when[^"]*"[^>]*>.*?</td>\s*<td[^>]*>(.*?)</td>', row, re.S)
    assert after_landed, 'drilldown row has no Landed cell'
    return after_landed.group(1)


class ProseBudget(WebTest):
    """The pages a budget is taken over, with one conviction per verdict so every category renders."""

    def _escape(self, number: int, test_name: str, verdict: str, runs_before: int = 98,
                failed_before: int = 0, runs_after: int = 108, failed_after: int = 7,
                recent_runs: Optional[int] = 259, recent_failed: Optional[int] = 0) -> None:
        build_id = self.store_build(number, flaky={test_name: config.CLEAN_TREE}, pr_id=number,
                                    pr_title=f'Change {number}')
        with self.connection:
            self.connection.execute(
                '''INSERT INTO escape_verdicts (
                    build_id, test_name, verdict, runs_before, failed_before, runs_after,
                    failed_after, landed_at, window_ends_at, decided_at, recent_runs, recent_failed,
                    recent_checked_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (build_id, test_name, verdict, runs_before, failed_before, runs_after, failed_after,
                 fixtures.DEFAULT_BUILD_TIME, int(time.time()), int(time.time()), recent_runs,
                 recent_failed, int(time.time()) if recent_runs is not None else None),
            )

    def one_of_every_verdict(self) -> None:
        """One conviction per verdict, plus a second build on the diverged one's pull request: a
        conviction is only TREE_DIVERGED because a later build superseded it, so a single build would
        have the page counting heads no real row of that category can have."""
        for number, verdict in enumerate(escapes.VERDICTS, start=1):
            self._escape(number, f'fast/{verdict.lower()}.html', verdict)
            if verdict == escapes.TREE_DIVERGED:
                self.store_build(number + len(escapes.VERDICTS),
                                 flaky={f'fast/{verdict.lower()}.html': config.CLEAN_TREE},
                                 pr_id=number, pr_title=f'Change {number}', sha='b' * 40,
                                 started_at=fixtures.DEFAULT_BUILD_TIME + 600)


class TestBlockBudgets(ProseBudget):
    def test_every_budgeted_block_fits_its_ceiling(self) -> None:
        self.one_of_every_verdict()
        for budget in BUDGETS:
            with self.subTest(budget.name):
                counted = len(words(block(self.page(budget.path), budget.opening, budget.tag)))
                self.assertLessEqual(
                    counted, budget.ceiling,
                    f'{budget.name} renders {counted} words, ceiling {budget.ceiling}. '
                    'Cut copy — do not raise the ceiling.')

    def test_a_stale_locator_fails_its_budget_instead_of_measuring_nothing(self) -> None:
        """A budget whose block was renamed away must go red, or the page it guarded is unbudgeted and
        the suite still passes."""
        self.one_of_every_verdict()
        with self.assertRaises(ValueError):
            block(self.page(ESCAPES), '<div class="a-block-this-page-does-not-have">', 'div')

    def test_a_failure_names_the_block_the_measurement_and_the_ceiling(self) -> None:
        """The message is the whole mechanism: a number to argue with, not an instruction to be
        tidier."""
        impossible = Budget('escapes legend', ESCAPES, '<details class="section legend-pane">',
                            'details', 1)
        counted = len(words(block(self.page(impossible.path), impossible.opening, impossible.tag)))
        message = (f'{impossible.name} renders {counted} words, ceiling {impossible.ceiling}')
        self.assertIn('escapes legend', message)
        self.assertIn(str(counted), message)
        self.assertGreater(counted, impossible.ceiling)


class TestListingRowBudget(ProseBudget):
    """A grouped row is a line of figures, not a description of the group behind it."""

    def test_no_listing_row_exceeds_the_row_ceiling(self) -> None:
        self.one_of_every_verdict()
        for category in escapes.CATEGORIES:
            page = self.page(f'{ESCAPES}?verdict={category}')
            for row in rows(page):
                counted = len(words(row))
                self.assertLessEqual(
                    counted, LISTING_ROW_WORDS,
                    f'a {category} listing row renders {counted} words, ceiling '
                    f'{LISTING_ROW_WORDS}. Cut copy — do not raise the ceiling.')

    def test_a_listing_row_prints_no_count_pair_at_all(self) -> None:
        """No pooling of the three pairs is honest over a group, so the row carries none of them and
        says how many convictions it stands for instead."""
        self._escape(1, 'fast/a.html', escapes.ESCAPED)
        row = rows(self.page(ESCAPES))[0]
        for pair in ('0/98', '7/108', '0/259'):
            self.assertNotIn(pair, row, pair)

    def test_no_drilldown_row_exceeds_the_row_ceiling(self) -> None:
        """One conviction's row is figures and link names, and it is the row a reason phrase is on, so
        it is the row prose would grow back into."""
        self.one_of_every_verdict()
        for category in escapes.CATEGORIES:
            page = self.page(f'{ESCAPES}?verdict={category}&test=fast/{category.lower()}.html')
            for row in drilldown_rows(page):
                counted = len(words(row))
                self.assertLessEqual(
                    counted, DRILLDOWN_ROW_WORDS,
                    f'a {category} drilldown row renders {counted} words, ceiling '
                    f'{DRILLDOWN_ROW_WORDS}. Cut copy — do not raise the ceiling.')

    def test_a_row_helper_whose_table_was_renamed_fails_instead_of_finding_no_rows(self) -> None:
        """Both ceilings above are taken over whatever these helpers return, so a helper that quietly
        returns nothing passes them on nothing."""
        self.one_of_every_verdict()
        page = self.page(ESCAPES)
        self.assertNotEqual(rows(page), [])
        with self.assertRaises(ValueError):
            _table_body(page, '<table class="a-table-this-page-does-not-have">')
        with self.assertRaises(ValueError):
            drilldown_rows(page)


class TestReasonColumnBudget(ProseBudget):
    """The column that replaced the Why sentence, which was 4,359 rendered words over 200 rows. It is
    in the drilldown now, where it describes one conviction rather than a group."""

    def test_no_cell_of_the_reason_column_exceeds_the_phrase_cap(self) -> None:
        self.one_of_every_verdict()
        for category in escapes.CATEGORIES:
            page = self.page(f'{ESCAPES}?verdict={category}'
                             f'&test=fast/{category.lower()}.html')
            for row in drilldown_rows(page):
                counted = len(words(reason_cell(row)))
                self.assertLessEqual(
                    counted, escapes.REASON_WORDS,
                    f'a {category} row renders {counted} words in the reason column, ceiling '
                    f'{escapes.REASON_WORDS}. Cut copy — do not raise the ceiling.')

    def test_the_escape_bucket_s_cell_is_the_baseline_pair_and_nothing_else(self) -> None:
        self._escape(1, 'fast/a.html', escapes.ESCAPED)
        cell = reason_cell(drilldown_rows(self.page(f"{ESCAPES}?test=fast/a.html"))[0])
        self.assertEqual(words(cell), ['0/98'])

    def test_a_category_main_answered_nothing_about_still_says_why(self) -> None:
        """An em dash in both numeric cells is not an answer, so these rows keep a phrase."""
        self.one_of_every_verdict()
        for category, expected in ((escapes.CONTAINED, 'Never failed it in 108 runs'),
                                   (escapes.NO_RUNS, 'No runs after the landing'),
                                   (escapes.NO_BASELINE, 'Failed 7 of 108, nothing before'),
                                   (escapes.TREE_DIVERGED, 'Built 2 times across 2 heads')):
            page = self.page(f'{ESCAPES}?verdict={category}'
                             f'&test=fast/{category.lower()}.html')
            cell = ' '.join(words(reason_cell(drilldown_rows(page)[0])))
            self.assertEqual(cell, expected, category)


class TestRowRepetition(ProseBudget):
    """No figure twice in one row. This is the check the Why column would have failed for months: it
    fitted no ceiling, and most of what it printed was a count printed again beside it."""

    def test_a_drilldown_row_prints_each_of_its_count_pairs_exactly_once(self) -> None:
        self._escape(1, 'fast/a.html', escapes.ESCAPED, runs_before=98, failed_before=0,
                     runs_after=108, failed_after=7, recent_runs=259, recent_failed=0)
        row = drilldown_rows(self.page(f'{ESCAPES}?test=fast/a.html'))[0]
        for pair in ('0/98', '7/108', '0/259'):
            self.assertEqual(row.count(pair), 1,
                             f'{pair} appears {row.count(pair)} times in one row')

    def test_the_counts_of_a_pair_are_not_restated_in_words_beside_it(self) -> None:
        """The prose this replaced said "7 of 108 runs after the landing" beside a cell already
        reading 7/108, and "none of its 259 runs" beside one reading 0/259."""
        self._escape(1, 'fast/a.html', escapes.ESCAPED)
        row = drilldown_rows(self.page(f'{ESCAPES}?test=fast/a.html'))[0]
        for restatement in ('7 of 108', '259 runs', 'after the landing'):
            self.assertNotIn(restatement, row, restatement)
