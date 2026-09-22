"""Word budgets for the blocks a reader scans, measured on the rendered page.

FIX A FAILURE HERE BY CUTTING COPY, NEVER BY RAISING THE CEILING. A ceiling moved to make the suite
green is a budget that has stopped measuring anything, and the block it guards is free to grow again.
Raising one is a deliberate edit with a reason written beside it, not a way past a red test.

Prose grows one clause at a time and every clause looks harmless, which is why this is a test and not
a note in a style guide. A reader has a small budget of attention, spends it scanning rather than
reading, and an explanation twice as long is usually less informative because it pushes the
load-bearing figure out of view. Two things are measured:

* every block in `BUDGETS` against its ceiling, and the widest single cell of the convictions table's
  fourth column, which is where 4,359 words of English used to live; and
* repetition — a row may print each of its three count pairs exactly once. The column that grew to
  4,359 words fitted no ceiling because no ceiling existed, and most of what it said was a figure
  printed again in the same row.

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

# The fourth cell of a conviction row: the baseline pair in the merged escape bucket, and a reason
# phrase in every category whose two numeric cells are em dashes. Found by position because both of
# its neighbours carry the classes it does.
REASON_COLUMN = 3


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
    # The three headline cards: their labels, values and denominators together. 43 when written.
    Budget('escapes headline cards', ESCAPES, '<div class="row metrics">', 'div', 50),
    # The splits under the escape bucket, which are labels beside counts rather than prose. 53.
    Budget('escapes significance split', ESCAPES, '<div class="subcategories">', 'div', 60),
    # The open category's own description, above the table and again in the pane's tooltip. ESCAPED's
    # is the longest. It was 117 words, most of them a rationale or a definition printed again in the
    # legend and the split tooltips; 47 after the cut this budget caught.
    Budget('escapes category meaning', ESCAPES, '<p class="meaning text tiny">', 'p', 60),
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


def rows(page: str) -> list:
    """Each conviction row of the listing, as its own HTML."""
    body = page.split('<tbody>')[1].split('</tbody>')[0]
    return [f'<tr>{fragment}' for fragment in body.split('<tr>')[1:]]


def cells(row: str) -> list:
    return re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)


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


class TestReasonColumnBudget(ProseBudget):
    """The column that replaced the Why sentence, which was 4,359 rendered words over 200 rows."""

    def test_no_cell_of_the_reason_column_exceeds_the_phrase_cap(self) -> None:
        self.one_of_every_verdict()
        for category in escapes.CATEGORIES:
            page = self.page(f'{ESCAPES}?verdict={category}')
            for row in rows(page):
                counted = len(words(cells(row)[REASON_COLUMN]))
                self.assertLessEqual(
                    counted, escapes.REASON_WORDS,
                    f'a {category} row renders {counted} words in the reason column, ceiling '
                    f'{escapes.REASON_WORDS}. Cut copy — do not raise the ceiling.')

    def test_the_escape_bucket_s_cell_is_the_baseline_pair_and_nothing_else(self) -> None:
        self._escape(1, 'fast/a.html', escapes.ESCAPED)
        cell = cells(rows(self.page(ESCAPES))[0])[REASON_COLUMN]
        self.assertEqual(words(cell), ['0/98'])

    def test_a_category_main_answered_nothing_about_still_says_why(self) -> None:
        """An em dash in both numeric cells is not an answer, so these rows keep a phrase."""
        self.one_of_every_verdict()
        for category, expected in ((escapes.CONTAINED, 'Never failed it in 108 runs'),
                                   (escapes.NO_RUNS, 'No runs after the landing'),
                                   (escapes.NO_BASELINE, 'Failed 7 of 108, nothing before'),
                                   (escapes.TREE_DIVERGED, 'Built 2 times across 2 heads')):
            page = self.page(f'{ESCAPES}?verdict={category}')
            cell = ' '.join(words(cells(rows(page)[0])[REASON_COLUMN]))
            self.assertEqual(cell, expected, category)


class TestRowRepetition(ProseBudget):
    """No figure twice in one row. This is the check the Why column would have failed for months: it
    fitted no ceiling, and most of what it printed was a count printed again beside it."""

    def test_a_row_prints_each_of_its_count_pairs_exactly_once(self) -> None:
        self._escape(1, 'fast/a.html', escapes.ESCAPED, runs_before=98, failed_before=0,
                     runs_after=108, failed_after=7, recent_runs=259, recent_failed=0)
        row = rows(self.page(ESCAPES))[0]
        for pair in ('0/98', '7/108', '0/259'):
            self.assertEqual(row.count(pair), 1,
                             f'{pair} appears {row.count(pair)} times in one row')

    def test_the_counts_of_a_pair_are_not_restated_in_words_beside_it(self) -> None:
        """The prose this replaced said "7 of 108 runs after the landing" beside a cell already
        reading 7/108, and "none of its 259 runs" beside one reading 0/259."""
        self._escape(1, 'fast/a.html', escapes.ESCAPED)
        row = rows(self.page(ESCAPES))[0]
        for restatement in ('7 of 108', '259 runs', 'after the landing'):
            self.assertNotIn(restatement, row, restatement)
