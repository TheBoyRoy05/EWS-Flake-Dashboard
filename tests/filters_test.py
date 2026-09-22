"""The filter vocabulary, which is the only door between a request and a WHERE or HAVING clause.

Every assertion here is about SQL text rather than about rows: what makes this module safe is that
the text it emits is assembled from literals in the module and the bind names it generates for
itself, so a value a reader typed can only ever arrive as a parameter.
"""

from __future__ import annotations

import unittest

from werkzeug.datastructures import MultiDict

from ews_dashboard import config
from ews_dashboard.analysis import filters

FILTER = filters.filter_argument(filters.TESTS)
SORT = filters.sort_argument(filters.TESTS)

# What a reader would have to type to reach the statement separator, if anything here interpolated.
PAYLOAD = '1); DROP TABLE build_verdicts; --'

# The literal expression each registered column stands for, repeated here rather than read off the
# registry: an expression that changed under the registry has to fail a test somewhere.
EXPRESSIONS = {
    'test': 'verdict.test_name',
    'rule': 'verdict.rule',
    'convictions': 'COUNT(*)',
    'queues': 'COUNT(DISTINCT build.builder)',
    'last_seen': 'MAX(build.started_at)',
    'queue': 'build.builder',
    'suite': 'build.suite',
    'pr': 'build.pr_id',
}

AGGREGATES = {'convictions', 'queues', 'last_seen'}

# The operators that bind no value, so a payload cannot reach them and every column keeps them.
VALUELESS = {'set', 'unset', 'off'}


def unescaped(value: object) -> str:
    """A bound value with the LIKE escaping removed, so a payload can be recognised whole: the
    payload's own underscore is escaped in a LIKE pattern and PAYLOAD carries no backslash of its
    own."""
    return str(value).replace('\\', '')


OPERATOR_NAMES = {
    filters.TEXT: {'eq', 'ne', 'has', 'nohas', 'starts', 'set', 'unset', 'off'},
    filters.INTEGER: {'eq', 'ne', 'gt', 'ge', 'lt', 'le', 'set', 'unset', 'off'},
    filters.TIMESTAMP: {'eq', 'ne', 'gt', 'ge', 'lt', 'le', 'set', 'unset', 'off'},
    filters.ENUM: {'eq', 'ne', 'in', 'notin', 'set', 'unset', 'off'},
    filters.ENUM_SET: {'anyof', 'allof', 'oneof', 'exactly', 'noneof', 'off'},
}

# The operators `rule` (the one `ENUM_SET` column) allows, none of which bind zero values.
SET_OPERATORS = OPERATOR_NAMES[filters.ENUM_SET] - {'off'}

# The clause each operator emits for the first condition of a set, whose bind names are `filter0_N`.
CLAUSES = {
    'eq': '{expression} = :filter0_0',
    'ne': '{expression} IS NOT :filter0_0',
    'gt': '{expression} > :filter0_0',
    'ge': '{expression} >= :filter0_0',
    'lt': '{expression} < :filter0_0',
    'le': '{expression} <= :filter0_0',
    'has': "{expression} LIKE :filter0_0 ESCAPE '\\'",
    'nohas': "{expression} NOT LIKE :filter0_0 ESCAPE '\\'",
    'starts': "{expression} LIKE :filter0_0 ESCAPE '\\'",
    'in': '{expression} IN (:filter0_0)',
    'notin': '{expression} NOT IN (:filter0_0)',
    'set': '{expression} IS NOT NULL',
    'unset': '{expression} IS NULL',
    'off': '',
    'anyof': 'SUM(CASE WHEN {expression} IN (:filter0_0) THEN 1 ELSE 0 END) > 0',
    'allof': 'COUNT(DISTINCT CASE WHEN {expression} IN (:filter0_0) THEN {expression} END) = 1',
    'oneof': 'COUNT(DISTINCT CASE WHEN {expression} IN (:filter0_0) THEN {expression} END) = 1',
    'exactly': ('COUNT(DISTINCT CASE WHEN {expression} IN (:filter0_0) THEN {expression} END) = 1 '
               'AND COUNT(DISTINCT {expression}) = 1'),
    'noneof': 'SUM(CASE WHEN {expression} IN (:filter0_0) THEN 1 ELSE 0 END) = 0',
}


class TestRegistry(unittest.TestCase):
    def test_every_column_stands_for_the_expression_it_is_registered_with(self) -> None:
        self.assertEqual({name: column.expression
                          for name, column in filters.TESTS.columns.items()}, EXPRESSIONS)

    def test_an_aggregate_orders_on_its_output_alias_and_filters_on_the_aggregate(self) -> None:
        """The counting query groups without selecting any aggregate, so it has no alias in scope;
        the page's own query selects all three under exactly these names."""
        for name in AGGREGATES:
            column = filters.TESTS.columns[name]
            self.assertTrue(column.aggregate, name)
            self.assertEqual(column.order_expression, name)
        self.assertEqual(filters.TESTS.columns['test'].order_expression, 'verdict.test_name')

    def test_each_kind_allows_exactly_the_operators_it_is_registered_with(self) -> None:
        # An aggregate is never NULL over a grouped row, so `set`/`unset` would emit
        # `HAVING COUNT(*) IS NOT NULL` — a clause that can never narrow anything — and are dropped.
        for column in filters.TESTS.columns.values():
            expected = OPERATOR_NAMES[column.kind]
            if column.name in AGGREGATES:
                expected = expected - {'set', 'unset'}
            self.assertEqual(set(column.operators), expected, column.name)


# The literal expression each escapes column stands for, repeated here rather than read off the
# registry for the same reason the tests table's are: an expression that changed under the registry
# has to fail a test somewhere. Rate increase and damage are the registered sqlite functions scaled to
# the percentage the page prints, so a reader filters against the number in the cell.
ESCAPE_EXPRESSIONS = {
    'test': 'outcome.test_name',
    'rule': 'verdict.rule',
    'queue': 'build.builder',
    'verdict': 'outcome.verdict',
    'landed': 'outcome.landed_at',
    'increase': ('escape_rate_increase(outcome.runs_before, outcome.failed_before, '
                 'outcome.runs_after, outcome.failed_after) * 100'),
    'damage': 'escape_damage(outcome.recent_runs, outcome.recent_failed) * 100',
    'runs_after': 'outcome.runs_after',
    'failed_after': 'outcome.failed_after',
}

# Which escapes columns a reader may order by. The three category names are deliberately absent:
# ordering a table by a bucket name ships a ranking the alphabet invented — TREE_DIVERGED before
# ESCAPED is not a severity — which is the judgment TESTS already makes for `rule`, `queue`, `suite`.
ESCAPE_SORTABLE = {'test', 'landed', 'increase', 'damage', 'runs_after', 'failed_after'}

# The escapes columns whose NULL means no evidence was gathered rather than a low value.
ESCAPE_NULLS_LAST = {'landed', 'increase', 'damage'}


class TestEscapesRegistry(unittest.TestCase):
    """The escapes listing's vocabulary, which is one row per (build, test) and never grouped."""

    def test_every_column_stands_for_the_expression_it_is_registered_with(self) -> None:
        self.assertEqual({name: column.expression
                          for name, column in filters.ESCAPES.columns.items()},
                         ESCAPE_EXPRESSIONS)

    def test_the_two_derived_columns_call_the_functions_the_page_derives_them_with(self) -> None:
        """Not a stored column and not the Newcombe formula re-spelled in SQL: one definition,
        called from the order and printed in the cell, so the two cannot disagree."""
        self.assertIn(f'{config.ESCAPE_INCREASE_FUNCTION}(',
                      filters.ESCAPES.columns['increase'].expression)
        self.assertIn(f'{config.ESCAPE_DAMAGE_FUNCTION}(',
                      filters.ESCAPES.columns['damage'].expression)

    def test_no_column_groups_so_a_clause_can_never_need_a_having(self) -> None:
        """This query has no GROUP BY, and a HAVING attached to it would read over the whole result as
        one group. Nothing in the registry can produce one: no aggregate column, and no ENUM_SET
        column to bring a `grouped` operator with it."""
        for column in filters.ESCAPES.columns.values():
            self.assertFalse(column.aggregate, column.name)
            self.assertNotEqual(column.kind, filters.ENUM_SET, column.name)
        for column in filters.ESCAPES.columns.values():
            for operator in column.operators.values():
                self.assertFalse(operator.grouped, f'{column.name}:{operator.name}')

    def test_only_the_columns_that_carry_no_grade_of_their_own_are_sortable(self) -> None:
        self.assertEqual(set(filters.ESCAPES.sortable_names), ESCAPE_SORTABLE)

    def test_rates_and_times_descend_first_and_a_test_name_ascends_first(self) -> None:
        self.assertFalse(filters.ESCAPES.columns['test'].descending_first)
        for name in ('landed', 'increase', 'damage', 'runs_after', 'failed_after'):
            self.assertTrue(filters.ESCAPES.columns[name].descending_first, name)

    def test_a_column_whose_null_means_no_evidence_sorts_those_rows_last(self) -> None:
        self.assertEqual({name for name, column in filters.ESCAPES.columns.items()
                          if column.nulls_last}, ESCAPE_NULLS_LAST)

    def test_the_tiebreak_is_the_table_s_primary_key(self) -> None:
        """escape_verdicts is keyed on (build_id, test_name), and this listing pages with
        LIMIT/OFFSET: an order that is not total drops a row from one page and repeats it on the
        next."""
        self.assertEqual(filters.ESCAPES.tiebreak,
                         'outcome.build_id DESC, outcome.test_name ASC')

    def test_the_verdict_vocabulary_is_the_one_the_escape_checker_decides_into(self) -> None:
        self.assertEqual(filters.ESCAPES.columns['verdict'].vocabulary, config.ESCAPE_VERDICTS)

    def test_a_verdict_outside_that_vocabulary_drops_the_whole_condition(self) -> None:
        self.assertIsNone(filters.condition(filters.ESCAPES, 'verdict', 'eq', 'ESCAPED_RARELY'))
        self.assertIsNotNone(filters.condition(filters.ESCAPES, 'verdict', 'eq', config.ESCAPED))

    def test_a_tests_column_is_not_reachable_through_the_escapes_table(self) -> None:
        """Two registries, not one with a union of both pages' columns: `convictions` and `queues`
        are aggregates of a grouped query this one does not run."""
        for name in ('convictions', 'queues', 'last_seen', 'suite', 'pr'):
            self.assertIsNone(filters.ESCAPES.column(name), name)


class TestNullPlacement(unittest.TestCase):
    """Where a row that gathered no evidence sorts, which sqlite's own default gets wrong ascending."""

    def _sql(self, table: filters.Table, specification: str) -> str:
        keys = filters.requested(MultiDict([(filters.sort_argument(table), specification)]),
                                 table).sort_keys
        self.assertEqual(len(keys), 1, specification)
        return keys[0].sql

    def test_a_nulls_last_column_pushes_them_last_in_both_directions(self) -> None:
        expression = ESCAPE_EXPRESSIONS['increase']
        self.assertEqual(self._sql(filters.ESCAPES, 'increase:desc'),
                         f'{expression} IS NULL ASC, {expression} DESC')
        self.assertEqual(self._sql(filters.ESCAPES, 'increase:asc'),
                         f'{expression} IS NULL ASC, {expression} ASC')

    def test_an_ordinary_column_emits_the_plain_term(self) -> None:
        self.assertEqual(self._sql(filters.ESCAPES, 'runs_after:desc'),
                         'outcome.runs_after DESC')
        self.assertEqual(self._sql(filters.TESTS, 'last_seen:asc'), 'last_seen ASC')

    def test_the_null_term_stays_inside_the_key_order_by_dedupes(self) -> None:
        """`order_by` drops a repeated column by name, so the extra term cannot escape its key and
        leave a stray `IS NULL` ordering the table ahead of the key a reader asked for."""
        keys = filters.sort_keys(filters.ESCAPES, (('increase', True), ('increase', False)))
        ordered = filters.order_by(filters.ESCAPES, keys)
        self.assertEqual(ordered.count('IS NULL'), 1)
        self.assertTrue(ordered.endswith(filters.ESCAPES.tiebreak))


class TestEscapesInjection(unittest.TestCase):
    """The same claim as `TestInjection`, against the second table: a payload either failed to coerce
    and dropped the condition, or it is in the parameters and the clause is this module's own
    template."""

    def test_a_payload_never_reaches_the_clause_of_any_escapes_column(self) -> None:
        for name, column in filters.ESCAPES.columns.items():
            for operator_name in column.operators:
                each = filters.condition(filters.ESCAPES, name, operator_name, PAYLOAD)
                if each is None:
                    continue
                where, having, parameters = filters.clause((each,))
                self.assertEqual(having, '', f'{name}:{operator_name}')
                self.assertNotIn('DROP', where, f'{name}:{operator_name}')
                if operator_name in VALUELESS:
                    self.assertEqual(parameters, {})
                    continue
                # `has`/`nohas`/`starts` bind the payload inside a LIKE pattern, so it is contained
                # in the bound value rather than equal to it — still bound, still nowhere else.
                self.assertTrue(any(PAYLOAD in unescaped(value) for value in parameters.values()),
                                f'{name}:{operator_name}')

    def test_a_column_that_is_not_in_the_escapes_registry_is_not_a_column(self) -> None:
        self.assertIsNone(filters.condition(filters.ESCAPES, PAYLOAD, 'eq', 'x'))
        self.assertIsNone(filters.condition(filters.ESCAPES, 'increase', PAYLOAD, '1'))


class TestEscapesRequested(unittest.TestCase):
    """`f.escapes=`/`s.escapes=` is a grammar of its own, read in written order like the tests one."""

    ESCAPE_FILTER = filters.filter_argument(filters.ESCAPES)
    ESCAPE_SORT = filters.sort_argument(filters.ESCAPES)

    def test_the_arguments_are_named_after_the_table(self) -> None:
        self.assertEqual((self.ESCAPE_FILTER, self.ESCAPE_SORT), ('f.escapes', 's.escapes'))

    def test_repeated_clauses_are_read_in_the_order_they_were_written(self) -> None:
        asked = filters.requested(MultiDict([
            (self.ESCAPE_FILTER, 'test:has:webgl'),
            (self.ESCAPE_FILTER, f'increase:ge:{50}'),
            (self.ESCAPE_SORT, 'landed:asc'),
            (self.ESCAPE_SORT, 'increase:desc'),
        ]), filters.ESCAPES)
        self.assertEqual([condition.column.name for condition in asked.conditions],
                         ['test', 'increase'])
        self.assertEqual([key.specification for key in asked.sort_keys],
                         ['landed:asc', 'increase:desc'])
        self.assertEqual(asked.rejected, ())
        self.assertTrue(asked.narrowing)

    def test_a_clause_this_table_cannot_read_is_reported_rather_than_applied(self) -> None:
        asked = filters.requested(MultiDict([
            (self.ESCAPE_FILTER, 'convictions:gt:2'),
            (self.ESCAPE_SORT, 'verdict:desc'),
            (self.ESCAPE_FILTER, 'test:has:webgl'),
        ]), filters.ESCAPES)
        self.assertEqual(asked.rejected, ('convictions:gt:2', 'verdict:desc'))
        self.assertEqual([condition.column.name for condition in asked.conditions], ['test'])

    def test_a_sort_naming_no_direction_reads_the_way_its_column_reads_first(self) -> None:
        asked = filters.requested(MultiDict([
            (self.ESCAPE_SORT, 'increase'), (self.ESCAPE_SORT, 'test'),
        ]), filters.ESCAPES)
        self.assertEqual([key.specification for key in asked.sort_keys],
                         ['increase:desc', 'test:asc'])


# Every operator whose clause describes the whole group a HAVING sees rather than one row a WHERE
# sees, on a column that is not itself an aggregate -- an aggregate column lands in HAVING for the
# separate reason that a WHERE runs before the grouping that produces it.
GROUPED_OPERATORS = {'anyof', 'allof', 'oneof', 'exactly', 'noneof'}


class TestInjection(unittest.TestCase):
    """A statement separator in a value, against every column and every operator its kind allows.

    Either the value did not coerce and the whole condition was dropped, or the clause is the
    template for that operator and the payload is in the parameters and nowhere else. There is no
    third outcome, which is the whole claim of this module.
    """

    def _clause(self, name: str, operator: str) -> tuple:
        parsed = filters.condition(filters.TESTS, name, operator, PAYLOAD)
        if parsed is None:
            return None, None, None
        where, having, parameters = filters.clause((parsed,))
        return where, having, parameters

    def test_a_statement_separator_reaches_the_parameters_and_never_the_sql(self) -> None:
        coerced = 0
        for name, column in filters.TESTS.columns.items():
            for operator in column.operators:
                where, having, parameters = self._clause(name, operator)
                if parameters is None:
                    self.assertIn(column.kind, (filters.INTEGER, filters.TIMESTAMP, filters.ENUM,
                                                filters.ENUM_SET),
                                  f'{name} {operator} accepted nothing and is not typed')
                    continue
                coerced += 1
                grouped = name in AGGREGATES or operator in GROUPED_OPERATORS
                sql = having if grouped else where
                self.assertEqual(sql, CLAUSES[operator].format(expression=EXPRESSIONS[name]),
                                 f'{name} {operator}')
                self.assertEqual('' if grouped else having, '', f'{name} {operator}')
                self.assertNotIn(PAYLOAD, sql, f'{name} {operator}')
                self.assertNotIn('DROP', sql, f'{name} {operator}')
                for value in parameters.values():
                    self.assertIn(PAYLOAD, unescaped(value), f'{name} {operator}')
        # Every column but `rule` and the three aggregates offers all three valueless operators;
        # `rule` (`ENUM_SET`) offers only `off`, since a set operator already covers what
        # `set`/`unset` would otherwise ask, and an aggregate loses `set`/`unset` for the same reason
        # (see test_each_kind_allows_exactly_the_operators_it_is_registered_with), leaving it `off`
        # alone too.
        plain_columns = len(filters.TESTS.columns) - len(AGGREGATES) - 1
        self.assertEqual(coerced, plain_columns * len(VALUELESS) + (len(AGGREGATES) + 1)
                         + len(OPERATOR_NAMES[filters.TEXT] - VALUELESS)
                         + len(OPERATOR_NAMES[filters.ENUM] - VALUELESS))

    def test_a_payload_that_will_not_coerce_leaves_no_condition_at_all(self) -> None:
        for name in ('convictions', 'last_seen', 'pr', 'suite'):
            self.assertIsNone(filters.condition(filters.TESTS, name, 'eq', PAYLOAD), name)


class TestParsing(unittest.TestCase):
    def test_a_column_that_is_not_registered_drops_the_row(self) -> None:
        """`builder` is the raw SQL column another table's condition would use, rather than the name
        this table registers it under, so it is a miss here like any other name a reader could
        invent."""
        self.assertIsNone(filters.condition(filters.TESTS, 'build.builder', 'eq', 'anything'))
        self.assertIsNone(filters.condition(filters.TESTS, 'builder', 'eq', 'anything'))

    def test_an_operator_that_is_not_an_operator_drops_the_row(self) -> None:
        self.assertIsNone(filters.condition(filters.TESTS, 'test', 'matches', 'fast/editing'))

    def test_an_operator_of_the_wrong_kind_drops_the_row_the_way_an_unknown_one_does(self) -> None:
        """A kind mismatch is a miss in the same dict, so `more than` on a test name and an operator
        nobody has written both fail without a branch of their own."""
        self.assertIsNone(filters.condition(filters.TESTS, 'test', 'gt', 'fast/editing'))
        self.assertIsNone(filters.condition(filters.TESTS, 'convictions', 'has', '4'))
        self.assertIsNone(filters.condition(filters.TESTS, 'suite', 'ge', 'layout-tests'))

    def test_a_value_of_the_wrong_type_drops_the_row(self) -> None:
        self.assertIsNone(filters.condition(filters.TESTS, 'convictions', 'ge', 'several'))
        self.assertIsNone(filters.condition(filters.TESTS, 'pr', 'eq', '12345x'))
        self.assertIsNone(filters.condition(filters.TESTS, 'last_seen', 'lt', '2026-13-01'))
        self.assertIsNone(filters.condition(filters.TESTS, 'suite', 'eq', 'unit-tests'))

    def test_a_timestamp_takes_an_epoch_second_or_a_date(self) -> None:
        _, having, parameters = filters.clause(
            filters.parse(filters.TESTS, (('last_seen', 'ge', '2026-08-01'),
                                          ('last_seen', 'lt', '1785628800'))))
        self.assertEqual(having, 'MAX(build.started_at) >= :filter0_0 AND '
                                'MAX(build.started_at) < :filter1_0')
        self.assertEqual(parameters, {'filter0_0': 1785542400, 'filter1_0': 1785628800})

    def test_a_missing_value_drops_a_row_that_needs_one_and_not_one_that_does_not(self) -> None:
        self.assertIsNone(filters.condition(filters.TESTS, 'test', 'has', None))
        self.assertIsNotNone(filters.condition(filters.TESTS, 'pr', 'unset', None))

    def test_parse_keeps_what_it_accepts_in_the_order_it_was_given(self) -> None:
        parsed = filters.parse(filters.TESTS, (
            ('queues', 'gt', '1'),
            ('nonsense', 'eq', 'dropped'),
            ('test', 'starts', 'fast/editing/'),
        ))
        self.assertEqual([each.column.name for each in parsed], ['queues', 'test'])


class TestLists(unittest.TestCase):
    def test_the_placeholder_count_is_the_count_of_values_that_coerced(self) -> None:
        queues = 'macOS-Sequoia-Release-WK2-Tests-EWS|GTK-Linux-64-bit-Release-Tests-EWS'
        where, _, parameters = filters.clause(
            filters.parse(filters.TESTS, (('queue', 'in', queues),)))
        self.assertEqual(where, 'build.builder IN (:filter0_0, :filter0_1)')
        self.assertEqual(parameters, {'filter0_0': 'macOS-Sequoia-Release-WK2-Tests-EWS',
                                      'filter0_1': 'GTK-Linux-64-bit-Release-Tests-EWS'})

    def test_an_element_that_is_not_in_the_vocabulary_is_dropped_from_the_list(self) -> None:
        where, _, parameters = filters.clause(
            filters.parse(filters.TESTS, (('suite', 'in', 'layout-tests|unit-tests'),)))
        self.assertEqual(where, 'build.suite IN (:filter0_0)')
        self.assertEqual(parameters, {'filter0_0': 'layout-tests'})

    def test_a_list_with_nothing_left_in_it_drops_the_row_rather_than_emitting_in(self) -> None:
        """`IN ()` is a syntax error in sqlite, and a list narrowed to nothing is not the same request
        as no list at all."""
        self.assertIsNone(filters.condition(filters.TESTS, 'suite', 'in', 'unit-tests|jsc-tests'))
        self.assertIsNone(filters.condition(filters.TESTS, 'suite', 'notin', ''))
        self.assertEqual(filters.clause(filters.parse(
            filters.TESTS, (('suite', 'in', 'unit-tests'),))), ('', '', {}))


# The templates the five set operators emit, restated here as a pin independent of the module: a
# `{placeholders}`/`{count}` pair fills in per test, since the count of values varies test to test.
SET_TEMPLATES = {
    'anyof': 'SUM(CASE WHEN {expression} IN ({placeholders}) THEN 1 ELSE 0 END) > 0',
    'allof': 'COUNT(DISTINCT CASE WHEN {expression} IN ({placeholders}) THEN {expression} END) '
            '= {count}',
    'oneof': 'COUNT(DISTINCT CASE WHEN {expression} IN ({placeholders}) THEN {expression} END) = 1',
    'exactly': ('COUNT(DISTINCT CASE WHEN {expression} IN ({placeholders}) THEN {expression} END) '
               '= {count} AND COUNT(DISTINCT {expression}) = {count}'),
    'noneof': 'SUM(CASE WHEN {expression} IN ({placeholders}) THEN 1 ELSE 0 END) = 0',
}


class TestSetOperators(unittest.TestCase):
    """`rule` is the only `ENUM_SET` column: its five operators describe the whole group a test's
    convictions were collapsed into rather than one row of it, so each has to land in HAVING even
    though `verdict.rule` is not itself an aggregate."""

    def _clause(self, operator: str, value: object) -> tuple:
        parsed = filters.condition(filters.TESTS, 'rule', operator, value)
        return filters.clause((parsed,))

    def test_each_set_operator_lands_in_having_and_not_where_with_one_bind_per_value(self) -> None:
        for operator in SET_OPERATORS:
            where, having, parameters = self._clause(
                operator, f'{config.CLEAN_TREE}|{config.DIRTY_TREE}')
            self.assertEqual(where, '', operator)
            self.assertEqual(having, SET_TEMPLATES[operator].format(
                expression='verdict.rule', placeholders=':filter0_0, :filter0_1', count=2), operator)
            self.assertEqual(parameters, {'filter0_0': config.CLEAN_TREE,
                                          'filter0_1': config.DIRTY_TREE}, operator)

    def test_count_reflects_the_values_that_coerced_and_a_dropped_element_changes_it(self) -> None:
        _, having, parameters = self._clause(
            'allof', f'{config.CLEAN_TREE}|{config.DIRTY_TREE}|not-a-rule')
        self.assertEqual(having, SET_TEMPLATES['allof'].format(
            expression='verdict.rule', placeholders=':filter0_0, :filter0_1', count=2))
        self.assertEqual(parameters, {'filter0_0': config.CLEAN_TREE,
                                      'filter0_1': config.DIRTY_TREE})

    def test_exactly_is_correct_with_a_single_value(self) -> None:
        _, having, parameters = self._clause('exactly', config.CLEAN_TREE)
        self.assertEqual(having, SET_TEMPLATES['exactly'].format(
            expression='verdict.rule', placeholders=':filter0_0', count=1))
        self.assertEqual(parameters, {'filter0_0': config.CLEAN_TREE})

    def test_a_repeated_value_is_bound_once_so_allof_and_exactly_stay_satisfiable(self) -> None:
        """Before the fix, `allof`/`exactly` compared `COUNT(DISTINCT ...)` against `{count}`, and
        `{count}` was `len(binds)` before the values behind those binds were deduplicated: a repeated
        value bound the same string twice, so `{count}` counted 2 while the distinct count could never
        pass 1, and the clause matched nothing regardless of the data. Reverting the dedup in
        `_values` (leaving `_SET`'s templates alone) reproduces exactly that: this test's `allof`
        assertion is the one that catches it, since it pins `count=1` and one bind rather than the
        `count=2`/two-bind clause the unfixed parser would have produced."""
        for operator in ('allof', 'exactly'):
            _, having, parameters = self._clause(
                operator, f'{config.CLEAN_TREE}|{config.CLEAN_TREE}')
            self.assertEqual(having, SET_TEMPLATES[operator].format(
                expression='verdict.rule', placeholders=':filter0_0', count=1), operator)
            self.assertEqual(parameters, {'filter0_0': config.CLEAN_TREE}, operator)

    def test_a_repeated_value_keeps_its_first_position_and_a_distinct_one_survives(self) -> None:
        """The ordinary, non-duplicate case: three values with one repeat dedup down to two, in the
        order they were first written, so a dedup that instead dropped a legitimate distinct value
        (or reordered the survivors) would fail this rather than only the duplicate-only case above."""
        _, having, parameters = self._clause(
            'anyof', f'{config.CLEAN_TREE}|{config.DIRTY_TREE}|{config.CLEAN_TREE}')
        self.assertEqual(having, SET_TEMPLATES['anyof'].format(
            expression='verdict.rule', placeholders=':filter0_0, :filter0_1', count=2))
        self.assertEqual(parameters, {'filter0_0': config.CLEAN_TREE,
                                      'filter0_1': config.DIRTY_TREE})

    def test_off_emits_no_clause_and_binds_nothing(self) -> None:
        self.assertEqual(self._clause('off', None), ('', '', {}))

    def test_a_value_outside_the_vocabulary_leaves_no_condition_at_all(self) -> None:
        self.assertIsNone(filters.condition(filters.TESTS, 'rule', 'anyof', 'not-a-rule'))


class TestClause(unittest.TestCase):
    def test_an_aggregate_goes_to_having_and_a_column_to_where(self) -> None:
        where, having, parameters = filters.clause(filters.parse(filters.TESTS, (
            ('test', 'has', 'editing'),
            ('convictions', 'ge', '3'),
            ('queue', 'eq', 'macOS-Tahoe-Debug-API-Tests-EWS'),
            ('queues', 'gt', '1'),
        )))
        self.assertEqual(where, "verdict.test_name LIKE :filter0_0 ESCAPE '\\' "
                                'AND build.builder = :filter2_0')
        self.assertEqual(having, 'COUNT(*) >= :filter1_0 AND COUNT(DISTINCT build.builder) '
                                 '> :filter3_0')
        self.assertEqual(parameters, {'filter0_0': '%editing%', 'filter1_0': 3,
                                      'filter2_0': 'macOS-Tahoe-Debug-API-Tests-EWS',
                                      'filter3_0': 1})

    def test_a_parked_row_emits_no_clause_and_binds_nothing(self) -> None:
        """`off` is how a reader keeps a condition they are not applying, so it has to be a row that
        exists and narrows nothing rather than a row that was deleted."""
        parsed = filters.parse(filters.TESTS, (('test', 'off', None), ('queues', 'off', None)))
        self.assertEqual([each.column.name for each in parsed], ['test', 'queues'])
        self.assertEqual(filters.clause(parsed), ('', '', {}))

    def test_a_parked_row_does_not_take_a_bind_name_from_the_rows_around_it(self) -> None:
        where, _, parameters = filters.clause(filters.parse(filters.TESTS, (
            ('test', 'off', None), ('test', 'eq', 'fast/editing/inserting.html'),
        )))
        self.assertEqual(where, 'verdict.test_name = :filter1_0')
        self.assertEqual(parameters, {'filter1_0': 'fast/editing/inserting.html'})

    def test_nothing_asked_for_emits_nothing(self) -> None:
        self.assertEqual(filters.clause(()), ('', '', {}))

    def test_a_string_is_not_a_condition(self) -> None:
        with self.assertRaises(TypeError):
            filters.clause(("verdict.test_name = 'x' OR 1=1",))

    def test_an_operator_of_the_wrong_kind_cannot_be_held_by_a_condition(self) -> None:
        with self.assertRaises(ValueError):
            filters.Condition(column=filters.TESTS.columns['test'],
                              operator=filters.OPERATORS[filters.INTEGER]['gt'],
                              values=('3',))

    def test_a_condition_holds_as_many_values_as_its_operator_binds(self) -> None:
        column = filters.TESTS.columns['suite']
        with self.assertRaises(ValueError):
            filters.Condition(column=column, operator=column.operators['in'], values=())
        with self.assertRaises(ValueError):
            filters.Condition(column=column, operator=column.operators['unset'],
                              values=('layout-tests',))


class TestLikePattern(unittest.TestCase):
    def test_the_backslash_is_escaped_before_the_wildcards_it_would_then_escape(self) -> None:
        """Escaping `%` and `_` first would leave the backslashes those escapes introduced to be
        doubled by the backslash pass, so `100%` would stop matching itself."""
        self.assertEqual(filters._like_fragment('100\\%_x'), '%100\\\\\\%\\_x%')

    def test_a_fragment_matches_anywhere_and_a_prefix_only_at_the_start(self) -> None:
        self.assertEqual(filters._like_fragment('editing'), '%editing%')
        self.assertEqual(filters._like_prefix('fast/editing/'), 'fast/editing/%')


class TestOrderBy(unittest.TestCase):
    def test_several_keys_order_as_asked_for_and_end_in_the_tiebreak(self) -> None:
        keys = filters.sort_keys(filters.TESTS, (('queues', True), ('convictions', False)))
        self.assertEqual(filters.order_by(filters.TESTS, keys),
                         'queues DESC, convictions ASC, verdict.test_name ASC')

    def test_a_key_that_is_not_a_column_drops_the_key_and_not_the_order(self) -> None:
        keys = filters.sort_keys(filters.TESTS, (('builder', True), ('last_seen', True)))
        self.assertEqual([key.column.name for key in keys], ['last_seen'])
        self.assertEqual(filters.order_by(filters.TESTS, keys),
                         'last_seen DESC, verdict.test_name ASC')

    def test_a_column_the_table_does_not_offer_for_sorting_drops_the_key(self) -> None:
        self.assertEqual(filters.sort_keys(filters.TESTS, (('queue', True), ('pr', False))), ())

    def test_asking_for_nothing_still_orders_totally(self) -> None:
        self.assertEqual(filters.order_by(filters.TESTS, ()),
                         'verdict.test_name ASC')

    def test_a_repeated_column_is_ordered_on_once(self) -> None:
        keys = filters.sort_keys(filters.TESTS, (('convictions', True), ('convictions', False)))
        self.assertEqual(filters.order_by(filters.TESTS, keys),
                         'convictions DESC, verdict.test_name ASC')


class TestRequested(unittest.TestCase):
    """The public grammar: `f.<table>=<column>:<condition>:<value>` and
    `s.<table>=<column>:<asc|desc>`, both repeatable, both read in the order they were written.

    Read through a multi-dict, because that is what preserves repetition: a mapping with one value
    per name would keep the last clause of each and lose the rest without saying so.
    """

    def requested(self, *arguments: tuple) -> filters.Requested:
        return filters.requested(MultiDict(arguments), filters.TESTS)

    def test_a_clause_becomes_a_condition_on_the_column_it_names(self) -> None:
        asked = self.requested((FILTER, 'test:has:editing'))
        self.assertEqual([each.column.name for each in asked.conditions], ['test'])
        self.assertEqual(filters.clause(asked.conditions)[0],
                         "verdict.test_name LIKE :filter0_0 ESCAPE '\\'")
        self.assertEqual(filters.clause(asked.conditions)[2], {'filter0_0': '%editing%'})

    def test_repeated_clauses_all_apply_in_the_order_they_were_written(self) -> None:
        asked = self.requested((FILTER, 'test:has:fast'), (FILTER, 'convictions:ge:3'),
                               (FILTER, 'test:nohas:forms'))
        self.assertEqual([each.operator.name for each in asked.conditions],
                         ['has', 'ge', 'nohas'])
        where, having, parameters = filters.clause(asked.conditions)
        self.assertEqual(where, "verdict.test_name LIKE :filter0_0 ESCAPE '\\' AND "
                                "verdict.test_name NOT LIKE :filter2_0 ESCAPE '\\'")
        self.assertEqual(having, 'COUNT(*) >= :filter1_0')
        self.assertEqual(parameters,
                         {'filter0_0': '%fast%', 'filter1_0': 3, 'filter2_0': '%forms%'})

    def test_two_sorts_are_a_primary_and_a_secondary_key_in_that_order(self) -> None:
        asked = self.requested((SORT, 'convictions:desc'), (SORT, 'test:asc'))
        self.assertEqual(filters.order_by(filters.TESTS, asked.sort_keys),
                         'convictions DESC, verdict.test_name ASC, verdict.test_name ASC')
        reversed_order = self.requested((SORT, 'test:asc'), (SORT, 'convictions:desc'))
        self.assertEqual(filters.order_by(filters.TESTS, reversed_order.sort_keys),
                         'verdict.test_name ASC, convictions DESC, verdict.test_name ASC')

    def test_a_sort_naming_no_direction_reads_the_way_its_column_reads_first(self) -> None:
        self.assertEqual(self.requested((SORT, 'test')).sort_keys[0].descending, False)
        self.assertEqual(self.requested((SORT, 'convictions')).sort_keys[0].descending, True)

    def test_the_clauses_to_hand_forward_are_the_readers_text_and_a_canonical_sort(self) -> None:
        """A heading link has to build a sort clause out of a column and a direction anyway, while
        respelling a filter would mean turning a LIKE pattern back into the fragment it came
        from."""
        asked = self.requested((FILTER, 'test:has:editing'), (SORT, 'convictions'))
        self.assertEqual(asked.filter_clauses, ('test:has:editing',))
        self.assertEqual(asked.sort_clauses, ('convictions:desc',))
        self.assertTrue(asked.narrowing)

    def test_a_value_keeps_the_separators_of_its_own(self) -> None:
        """Split at most twice, so a name holding a colon is one value and not a broken clause."""
        asked = self.requested((FILTER, 'test:eq:imported/w3c/a:b.html'))
        self.assertEqual(filters.clause(asked.conditions)[2],
                         {'filter0_0': 'imported/w3c/a:b.html'})
        self.assertEqual(asked.rejected, ())

    def test_a_clause_that_does_not_parse_is_rejected_and_the_rest_still_apply(self) -> None:
        asked = self.requested((FILTER, 'nonsense:eq:1'), (FILTER, 'test:has:editing'),
                               (FILTER, 'test:gt:editing'), (FILTER, 'convictions:ge:several'),
                               (FILTER, 'test'), (SORT, 'queue:asc'), (SORT, 'test:sideways'))
        self.assertEqual([each.column.name for each in asked.conditions], ['test'])
        self.assertEqual(asked.sort_keys, ())
        self.assertEqual(asked.rejected, ('nonsense:eq:1', 'test:gt:editing',
                                          'convictions:ge:several', 'test',
                                          'queue:asc', 'test:sideways'))

    def test_an_argument_holding_nothing_is_neither_a_condition_nor_a_mistake(self) -> None:
        """Every submission of the filter surface sends the empty field it offers for the next
        clause, so reporting one would tell a reader they got something wrong by using the form."""
        asked = self.requested((FILTER, ''), (FILTER, '   '), (SORT, ''))
        self.assertEqual((asked.conditions, asked.sort_keys, asked.rejected), ((), (), ()))
        self.assertFalse(asked.narrowing)

    def test_an_off_condition_alone_does_not_narrow_but_mixed_with_a_real_one_does(self) -> None:
        """`off` is a committed condition with no template at all, kept only so a chip can be turned
        back on — it must not read as having narrowed anything by itself."""
        self.assertFalse(self.requested((FILTER, 'rule:off')).narrowing)
        mixed = self.requested((FILTER, 'rule:off'), (FILTER, 'test:has:editing'))
        self.assertTrue(mixed.narrowing)

    def test_an_empty_value_drops_a_clause_that_needs_one_and_not_one_that_does_not(self) -> None:
        self.assertEqual(self.requested((FILTER, 'test:has:')).rejected, ('test:has:',))
        self.assertEqual([each.operator.name
                          for each in self.requested((FILTER, 'pr:unset')).conditions], ['unset'])

    def test_asking_for_nothing_asks_for_nothing(self) -> None:
        self.assertEqual(filters.requested(MultiDict(), filters.TESTS), filters.Requested())

    def test_another_tables_arguments_are_not_this_ones(self) -> None:
        """The argument names the table, so one page can filter two tables independently."""
        self.assertEqual(self.requested(('f.other', 'test:has:editing')), filters.Requested())


class TestExplodedChips(unittest.TestCase):
    """The chip form's second door: `f.<table>:<index>:column`/`op`/`value` and
    `s.<table>:<index>:column`/`direction`, exploded because a form with no script cannot compose one
    canonical clause out of three controls. Nothing here is a new validation path — every spec this
    reads still goes through `condition`/`requested` before it narrows anything."""

    def has_exploded(self, *arguments: tuple) -> bool:
        return filters.has_exploded_arguments(MultiDict(arguments), filters.TESTS)

    def filter_specs(self, *arguments: tuple) -> tuple:
        return filters.exploded_filter_specifications(MultiDict(arguments), filters.TESTS)

    def sort_specs(self, *arguments: tuple) -> tuple:
        return filters.exploded_sort_specifications(MultiDict(arguments), filters.TESTS)

    def test_two_filter_chips_round_trip_to_two_canonical_specs_in_order(self) -> None:
        specs = self.filter_specs(
            (filters.filter_chip_argument(filters.TESTS, 0, 'column'), 'test'),
            (filters.filter_chip_argument(filters.TESTS, 0, 'op'), 'has'),
            (filters.filter_chip_argument(filters.TESTS, 0, 'value'), 'fast'),
            (filters.filter_chip_argument(filters.TESTS, 1, 'column'), 'convictions'),
            (filters.filter_chip_argument(filters.TESTS, 1, 'op'), 'ge'),
            (filters.filter_chip_argument(filters.TESTS, 1, 'value'), '3'))
        self.assertEqual(specs, ('test:has:fast', 'convictions:ge:3'))
        asked = filters.requested(MultiDict(zip((filters.filter_argument(filters.TESTS),) * 2,
                                                specs)), filters.TESTS)
        self.assertEqual([each.column.name for each in asked.conditions],
                         ['test', 'convictions'])

    def test_a_chip_with_no_column_is_dropped_rather_than_asked_about(self) -> None:
        specs = self.filter_specs(
            (filters.filter_chip_argument(filters.TESTS, 0, 'column'), ''),
            (filters.filter_chip_argument(filters.TESTS, 0, 'op'), 'has'),
            (filters.filter_chip_argument(filters.TESTS, 0, 'value'), 'fast'))
        self.assertEqual(specs, ())

    def test_a_chip_naming_a_column_this_table_does_not_have_is_dropped(self) -> None:
        specs = self.filter_specs(
            (filters.filter_chip_argument(filters.TESTS, 0, 'column'), 'nonsense'),
            (filters.filter_chip_argument(filters.TESTS, 0, 'op'), 'eq'),
            (filters.filter_chip_argument(filters.TESTS, 0, 'value'), '1'))
        self.assertEqual(specs, ())

    def test_a_valueless_operator_yields_a_spec_with_no_trailing_value(self) -> None:
        specs = self.filter_specs(
            (filters.filter_chip_argument(filters.TESTS, 0, 'column'), 'pr'),
            (filters.filter_chip_argument(filters.TESTS, 0, 'op'), 'unset'),
            (filters.filter_chip_argument(filters.TESTS, 0, 'value'), ''))
        self.assertEqual(specs, ('pr:unset',))

    def test_indexes_out_of_order_still_resolve_by_index(self) -> None:
        specs = self.filter_specs(
            (filters.filter_chip_argument(filters.TESTS, 1, 'value'), 'forms'),
            (filters.filter_chip_argument(filters.TESTS, 0, 'value'), 'fast'),
            (filters.filter_chip_argument(filters.TESTS, 1, 'op'), 'nohas'),
            (filters.filter_chip_argument(filters.TESTS, 0, 'op'), 'has'),
            (filters.filter_chip_argument(filters.TESTS, 1, 'column'), 'test'),
            (filters.filter_chip_argument(filters.TESTS, 0, 'column'), 'test'))
        self.assertEqual(specs, ('test:has:fast', 'test:nohas:forms'))

    def test_a_sort_chip_round_trips_to_its_canonical_spec(self) -> None:
        specs = self.sort_specs(
            (filters.sort_chip_argument(filters.TESTS, 0, 'column'), 'convictions'),
            (filters.sort_chip_argument(filters.TESTS, 0, 'direction'), 'desc'))
        self.assertEqual(specs, ('convictions:desc',))

    def test_has_exploded_arguments_is_false_for_the_canonical_grammar(self) -> None:
        self.assertFalse(self.has_exploded(
            (filters.filter_argument(filters.TESTS), 'test:has:editing')))
        self.assertTrue(self.has_exploded(
            (filters.filter_chip_argument(filters.TESTS, 0, 'column'), 'test')))
        self.assertTrue(self.has_exploded(
            (filters.sort_chip_argument(filters.TESTS, 0, 'column'), 'test')))

    def test_a_value_that_will_not_coerce_drops_that_chip_once_requested_reads_it(self) -> None:
        """The exploded reader only builds the spec; `condition`/`requested` is still the one place
        that decides whether a value coerces, so this composes rather than duplicating that check."""
        specs = self.filter_specs(
            (filters.filter_chip_argument(filters.TESTS, 0, 'column'), 'convictions'),
            (filters.filter_chip_argument(filters.TESTS, 0, 'op'), 'ge'),
            (filters.filter_chip_argument(filters.TESTS, 0, 'value'), 'several'))
        self.assertEqual(specs, ('convictions:ge:several',))
        asked = filters.requested(
            MultiDict(((filters.filter_argument(filters.TESTS), specs[0]),)), filters.TESTS)
        self.assertEqual(asked.conditions, ())
        self.assertEqual(asked.rejected, ('convictions:ge:several',))
