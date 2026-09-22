#!/usr/bin/env python3
"""Plot the escape-strength distribution against the proposed rate-increase score.

Run it:
    pip3 install matplotlib
    python3 -m scripts.plot_rate_increase

Optional arguments:
    --db PATH     the dashboard database (default: the repository's own ews-dashboard.db,
                  or EWS_DASHBOARD_DATABASE when that is set)
    --save PATH   also write the figure to a file instead of only showing it

Read-only: the database is opened through a `file:...?mode=ro` URI, so it cannot be written to even by
accident. This is a measurement script and lives outside the repository on purpose — the dashboard
itself takes no dependencies beyond Flask, and matplotlib is not one of them.

What the two scores are:

    escape strength   the lower end of the 90% Wilson interval on failed_after / runs_after, which is
                      what the escapes table prints today. It says how hard main failed the test in
                      the window after the landing. It cannot see the baseline at all.

    rate increase     the Newcombe square-and-add lower bound on p_after - p_before, proposed. It says
                      how much the landing changed the failure rate, so a test main was already
                      failing at the same rate scores near zero however badly it fails.
"""
import argparse
import math
import os
import sqlite3
import sys

Z = 1.6448536269514722          # the 90% two-sided z the dashboard already uses
BUCKET = ('ESCAPED', 'FAILS_ON_MAIN')


def wilson(runs, failed):
    """(lower, upper) of the 90% Wilson score interval on failed/runs; None with no runs."""
    if not runs:
        return None
    n = float(runs)
    p = failed / n
    denominator = 1 + Z * Z / n
    centre = p + Z * Z / (2 * n)
    margin = Z * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n))
    return (max(0.0, (centre - margin) / denominator),
            min(1.0, (centre + margin) / denominator))


def strength(runs_after, failed_after):
    got = wilson(runs_after, failed_after)
    return None if got is None else got[0]


def increase(runs_before, failed_before, runs_after, failed_after):
    """None when either side has no runs: no baseline means no change to measure, which is an absence
    of evidence rather than an increase of zero."""
    if not runs_after or not runs_before:
        return None
    after = wilson(runs_after, failed_after)
    before = wilson(runs_before, failed_before)
    p_after = failed_after / float(runs_after)
    p_before = failed_before / float(runs_before)
    return (p_after - p_before) - math.sqrt((p_after - after[0]) ** 2 +
                                            (before[1] - p_before) ** 2)


def default_database():
    """The same database the app reads: the environment override if set, else the one beside the
    repository root, so this is run from a checkout without arguments."""
    override = os.environ.get('EWS_DASHBOARD_DATABASE')
    if override:
        return override
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, 'ews-dashboard.db')


def load(path):
    uri = f'file:{path}?mode=ro'
    with sqlite3.connect(uri, uri=True) as conn:
        rows = conn.execute(
            'SELECT runs_before, failed_before, runs_after, failed_after '
            f'FROM escape_verdicts WHERE verdict IN ({",".join("?" * len(BUCKET))})',
            BUCKET).fetchall()
    scored = []
    for runs_before, failed_before, runs_after, failed_after in rows:
        scored.append((strength(runs_after, failed_after),
                       increase(runs_before, failed_before, runs_after, failed_after)))
    return scored


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', default=default_database())
    parser.add_argument('--save', default=None)
    args = parser.parse_args()

    try:
        import matplotlib
        if args.save:
            matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        sys.exit('matplotlib is not installed. Run: pip3 install matplotlib')

    if not os.path.exists(args.db):
        sys.exit(f'no database at {args.db} — pass --db PATH')

    scored = load(args.db)
    if not scored:
        sys.exit('no convictions in the merged bucket; has the escape pass run?')

    strengths = [s * 100 for s, _ in scored if s is not None]
    increases = [i * 100 for _, i in scored if i is not None]
    pairs = [(s * 100, i * 100) for s, i in scored if s is not None and i is not None]
    below = sum(1 for i in increases if i <= 0)

    print(f'convictions in the merged bucket: {len(scored):,}')
    print(f'  scored by strength: {len(strengths):,}')
    print(f'  scored by increase: {len(increases):,}')
    print(f'  increase at or below 0: {below:,} '
          f'({100.0 * below / len(increases):.1f}%) — no measurable worsening')
    print(f'  strength above 50%: {sum(1 for s in strengths if s > 50):,}')
    print(f'  increase above 50%: {sum(1 for i in increases if i > 50):,}')

    plt.style.use('dark_background')
    figure, axes = plt.subplots(2, 2, figsize=(14, 9))
    figure.suptitle(f'Escape strength against the proposed rate increase — '
                    f'{len(scored):,} convictions in the merged ESCAPED bucket', fontsize=13)
    old, new = '#6ba4f8', '#f78166'

    top_left = axes[0][0]
    top_left.hist(strengths, bins=range(0, 105, 5), color=old, edgecolor='#0d1117')
    top_left.set_yscale('log')
    top_left.set_title('Escape strength today (Wilson bound on the after-rate)', fontsize=10)
    top_left.set_xlabel('per cent')
    top_left.set_ylabel('convictions (log)')

    top_right = axes[0][1]
    top_right.hist(increases, bins=range(-100, 105, 5), color=new, edgecolor='#0d1117')
    top_right.set_yscale('log')
    top_right.axvline(0, color='#d29922', linestyle='--', linewidth=1)
    top_right.set_title('Proposed rate increase (Newcombe bound on after minus before)',
                        fontsize=10)
    top_right.set_xlabel('per cent')
    top_right.set_ylabel('convictions (log)')

    bottom_left = axes[1][0]
    for label, values, colour in (('escape strength', strengths, old),
                                  ('rate increase', increases, new)):
        ordered = sorted(values)
        share = [100.0 * (n + 1) / len(ordered) for n in range(len(ordered))]
        bottom_left.plot(ordered, share, color=colour, linewidth=2, label=label)
    bottom_left.axvline(0, color='#d29922', linestyle='--', linewidth=1)
    bottom_left.set_title('Cumulative share of convictions at or below a score', fontsize=10)
    bottom_left.set_xlabel('per cent')
    bottom_left.set_ylabel('share of convictions (%)')
    bottom_left.legend(loc='lower right', fontsize=9)

    bottom_right = axes[1][1]
    worsened = [(s, i) for s, i in pairs if i > 0]
    flat = [(s, i) for s, i in pairs if i <= 0]
    bottom_right.scatter([s for s, _ in flat], [i for _, i in flat], s=7, alpha=0.35,
                         color='#8b949e', label='no measurable worsening')
    bottom_right.scatter([s for s, _ in worsened], [i for _, i in worsened], s=7, alpha=0.6,
                         color=new, label='worsened after the landing')
    bottom_right.axhline(0, color='#d29922', linestyle='--', linewidth=1)
    bottom_right.set_title('The two scores per conviction', fontsize=10)
    bottom_right.set_xlabel('escape strength today (%)')
    bottom_right.set_ylabel('proposed rate increase (%)')
    bottom_right.legend(loc='lower right', fontsize=9)

    figure.tight_layout()
    if args.save:
        figure.savefig(args.save, dpi=150, facecolor=figure.get_facecolor())
        print(f'wrote {args.save}')
    else:
        plt.show()


if __name__ == '__main__':
    main()
