"""`queues.resolve` is the one place a reader's group/version/builder choice becomes a concrete
builder tuple; the analysis layer never re-derives group membership itself."""

from __future__ import annotations

import unittest

from ews_dashboard import queues
from tests import fixtures

KNOWN = (fixtures.LAYOUT_BUILDER, fixtures.API_BUILDER, fixtures.GTK_BUILDER, fixtures.WPE_BUILDER)

# One builder per family, so a family filter that reached the wrong one shows up. `Win-Tests-EWS` and
# `visionOS-26-Simulator-WK2-Tests-EWS` are the real queue names the group patterns were written
# against, and neither starts with its group's name.
IOS_BUILDER = fixtures.IOS_BUILDER
WINDOWS_BUILDER = 'Win-Tests-EWS'
VISION_BUILDER = 'visionOS-26-Simulator-WK2-Tests-EWS'
EVERY_FAMILY = (fixtures.LAYOUT_BUILDER, IOS_BUILDER, VISION_BUILDER, fixtures.GTK_BUILDER,
                fixtures.WPE_BUILDER, WINDOWS_BUILDER)


def resolved(**selection: object) -> tuple:
    return queues.resolve(queues.Selection(**selection), KNOWN)


class TestResolve(unittest.TestCase):
    def test_a_single_group_narrows_to_that_groups_builders(self) -> None:
        self.assertEqual(resolved(groups=('GTK',)), (fixtures.GTK_BUILDER,))

    def test_two_groups_selected_yields_the_union(self) -> None:
        one_group = resolved(groups=('GTK',))
        two_groups = resolved(groups=('GTK', 'WPE'))
        self.assertEqual(set(two_groups), {fixtures.GTK_BUILDER, fixtures.WPE_BUILDER})
        self.assertGreater(len(two_groups), len(one_group))

    def test_no_group_selected_returns_everything_the_unfiltered_query_does(self) -> None:
        self.assertEqual(resolved(), ())
        self.assertEqual(resolved(groups=()), ())

    def test_an_unrecognised_group_is_dropped_rather_than_reaching_sql(self) -> None:
        self.assertEqual(resolved(groups=('not-a-queue',)), ())
        self.assertEqual(resolved(groups=('GTK', 'not-a-queue')), (fixtures.GTK_BUILDER,))


class TestFamilies(unittest.TestCase):
    """The level above the groups. It has to be a partition of them: a group under two families would
    be selected twice by "everything", and a group under none would be unreachable from the top of the
    tree -- the same failure the group patterns were written to end."""

    def test_every_group_sits_under_exactly_one_family(self) -> None:
        held = [group for family in queues.QUEUE_FAMILIES for group in family.groups]
        self.assertEqual(sorted(held), sorted(set(held)))
        self.assertEqual(set(held), set(queues.QUEUE_GROUP_NAMES))

    def test_a_group_named_by_two_families_is_refused_rather_than_taking_the_last(self) -> None:
        both = (queues.QueueFamily('Apple', ('macOS',)), queues.QueueFamily('Linux', ('macOS',)))
        with self.assertRaises(AssertionError):
            queues._family_of_group(both)

    def test_a_group_no_family_holds_is_refused_rather_than_left_orphaned(self) -> None:
        with self.assertRaises(AssertionError):
            queues._family_of_group((queues.QueueFamily('Apple', ('macOS',)),))

    def test_a_family_naming_a_group_that_does_not_exist_is_refused(self) -> None:
        registry = queues.QUEUE_FAMILIES + (queues.QueueFamily('Solaris', ('SunOS',)),)
        with self.assertRaises(AssertionError):
            queues._family_of_group(registry)

    def test_each_group_reports_the_family_the_registry_puts_it_under(self) -> None:
        self.assertEqual(queues.family_of('macOS'), 'Apple')
        self.assertEqual(queues.family_of('iOS'), 'Apple')
        self.assertEqual(queues.family_of('visionOS'), 'Apple')
        self.assertEqual(queues.family_of('GTK'), 'Linux')
        self.assertEqual(queues.family_of('WPE'), 'Linux')
        self.assertEqual(queues.family_of('Windows'), 'Windows')
        self.assertEqual(queues.family_of(queues.OTHER), 'Other')

    def test_a_name_that_is_not_a_group_has_no_family_rather_than_a_guessed_one(self) -> None:
        self.assertIsNone(queues.family_of('not-a-queue'))


class TestResolveFamilies(unittest.TestCase):
    def resolved(self, **selection: object) -> tuple:
        return queues.resolve(queues.Selection(**selection), EVERY_FAMILY)

    def test_a_family_reaches_every_builder_of_every_group_it_holds(self) -> None:
        self.assertEqual(set(self.resolved(families=('Apple',))),
                         {fixtures.LAYOUT_BUILDER, IOS_BUILDER, VISION_BUILDER})
        self.assertEqual(set(self.resolved(families=('Linux',))),
                         {fixtures.GTK_BUILDER, fixtures.WPE_BUILDER})
        self.assertEqual(self.resolved(families=('Windows',)), (WINDOWS_BUILDER,))

    def test_the_families_together_reach_every_queue_and_none_of_them_twice(self) -> None:
        """A partition read back through `resolve`: every builder selected, each exactly once."""
        reached = self.resolved(families=queues.QUEUE_FAMILY_NAMES)
        self.assertEqual(reached, EVERY_FAMILY)

    def test_a_family_and_a_group_are_unioned_rather_than_intersected(self) -> None:
        self.assertEqual(set(self.resolved(families=('Linux',), groups=('Windows',))),
                         {fixtures.GTK_BUILDER, fixtures.WPE_BUILDER, WINDOWS_BUILDER})

    def test_an_unrecognised_family_is_dropped_rather_than_reaching_sql(self) -> None:
        self.assertEqual(self.resolved(families=('Solaris',)), ())
        self.assertEqual(self.resolved(families=('Linux', 'Solaris')),
                         (fixtures.GTK_BUILDER, fixtures.WPE_BUILDER))

    def test_no_family_selected_is_still_no_filter_at_all(self) -> None:
        self.assertEqual(self.resolved(families=()), ())
        self.assertTrue(queues.Selection().empty)
        self.assertFalse(queues.Selection(families=('Apple',)).empty)

    def test_a_group_selected_on_its_own_still_narrows_to_that_group(self) -> None:
        """The level was added above the groups, not in place of them: a link written before it
        existed must select exactly what it always did."""
        self.assertEqual(self.resolved(groups=('GTK',)), (fixtures.GTK_BUILDER,))
        self.assertEqual(self.resolved(versions=(('iOS', '18'),)), (IOS_BUILDER,))
        self.assertEqual(self.resolved(builders=(WINDOWS_BUILDER,)), (WINDOWS_BUILDER,))


class TestTree(unittest.TestCase):
    COUNTS = {fixtures.LAYOUT_BUILDER: 5, IOS_BUILDER: 3, VISION_BUILDER: 1,
              fixtures.GTK_BUILDER: 7, fixtures.WPE_BUILDER: 11, WINDOWS_BUILDER: 2}

    def tree(self, builders: tuple = EVERY_FAMILY) -> tuple:
        return queues.tree(builders, self.COUNTS)

    def named(self, nodes: tuple) -> list:
        return [node.name for node in nodes]

    def test_the_top_level_is_the_families_in_registry_order(self) -> None:
        self.assertEqual(self.named(self.tree()), ['Apple', 'Linux', 'Windows'])

    def test_a_family_holds_its_own_groups_and_the_group_level_survives(self) -> None:
        apple, linux, windows = self.tree()
        self.assertEqual(self.named(apple.groups), ['macOS', 'iOS', 'visionOS'])
        self.assertEqual(self.named(linux.groups), ['GTK', 'WPE'])
        self.assertEqual(self.named(windows.groups), ['Windows'])

    def test_a_version_still_divides_a_group_that_two_versions_reach(self) -> None:
        apple = self.tree((fixtures.LAYOUT_BUILDER, fixtures.API_BUILDER))[0]
        macos = apple.groups[0]
        self.assertEqual([version.version for version in macos.versions], ['Sequoia', 'Tahoe'])

    def test_a_family_with_none_of_these_builders_is_left_out_like_an_empty_group(self) -> None:
        self.assertEqual(self.named(self.tree((fixtures.GTK_BUILDER,))), ['Linux'])

    def test_a_count_beside_a_family_is_the_sum_of_what_it_contains(self) -> None:
        apple, linux, windows = self.tree()
        self.assertEqual(apple.convictions, sum(node.convictions for node in apple.groups))
        self.assertEqual(apple.convictions, 5 + 3 + 1)
        self.assertEqual(linux.convictions, 7 + 11)
        self.assertEqual(windows.convictions, 2)

    def test_every_count_folds_up_from_the_builder_leaves_and_none_is_counted_twice(self) -> None:
        """One definition, read at three levels: a version sums its builders, a group sums its
        versions, a family sums its groups."""
        apple = self.tree((fixtures.LAYOUT_BUILDER, fixtures.API_BUILDER, IOS_BUILDER))[0]
        macos = apple.groups[0]
        for version in macos.versions:
            self.assertEqual(version.convictions,
                             sum(leaf.convictions for leaf in version.builders))
        self.assertEqual(macos.convictions, sum(node.convictions for node in macos.versions))
        self.assertEqual(apple.convictions, sum(node.convictions for node in apple.groups))

    def test_a_family_of_one_group_of_its_own_name_renders_as_one_row(self) -> None:
        """`Windows` under `Windows` says nothing the parent did not, so the picker draws the family
        row alone and hangs that group's builders under it."""
        windows = self.tree((WINDOWS_BUILDER,))[0]
        self.assertIsNotNone(windows.sole_group)
        self.assertEqual(windows.sole_group.builders[0].builder, WINDOWS_BUILDER)

    def test_a_family_that_genuinely_divides_keeps_its_group_rows(self) -> None:
        apple, linux, _ = self.tree()
        self.assertIsNone(apple.sole_group)
        self.assertIsNone(linux.sole_group)

    def test_a_family_holding_one_group_of_another_name_keeps_that_group_row(self) -> None:
        """Apple with only iOS in the window is still a family above a group, and collapsing it would
        label the row `Apple` while selecting only iOS."""
        apple = self.tree((IOS_BUILDER,))[0]
        self.assertIsNone(apple.sole_group)
