# ews-flake-detection-dashboard

Does EWS's flake detection stop authors being blamed for failures that were not theirs?

EWS records every test failure and flake it sees to results.webkit.org, then consults that history
before blaming a change. This dashboard measures whether that helps. It answers one question on the
front page — what share of failing builds showed an author a failure that main already fails — and
links out to results.webkit.org and EWS for everything else, because both already render this data
better than a second copy would.

## Running it

```
pip3 install -r requirements.txt
python3 -m ews_dashboard.db                 # create the database
python3 -m scripts.refresh --days 14        # ingest builds and classify them (slow, needs network)
python3 -m flask --app ews_dashboard.web.app:create_app run
```

No credentials of any kind. Both APIs it reads are public and it only ever issues GETs, so there is
nothing to configure and nothing to leak. `EWS_DASHBOARD_DATABASE` overrides where the sqlite file
lives.

`scripts/refresh.py` is the only thing that touches the network. The web app reads the database and
nothing else, which is why a page cannot hang on a slow results.webkit.org query and why every page
shows how old its numbers are.

## How the metric is defined

A build's author-visible failures are the tests that failed in both the first run and the rerun and
did not fail on a clean tree — the set behind "Found N new test failures". Each is looked up on main
at the commit the change was rebased onto:

- **pre-existing** — main passes it 80% of the time or less, so the change is not the cause
- **real** — main passes it reliably, so the change is the likely cause
- **undetermined** — nothing recorded for that test in that configuration

Every rate on the pages is a floor, not a verdict. Main does not contain the change, so a test that
is reliable on main and genuinely broken by the change looks identical to one that flaked once during
this build. Read a trend, not a single build.

The filtered failure lists EWS publishes — `first_run_failures_filtered` and `second_run_failures_filtered`, which hold what the author was actually shown — only exist on builds from after the EWS deployments `config.py` dates: 2026-08-14 for the write path and 2026-08-25 for the read path, the first build carrying a results-db flakiness property. A layout-test build from before those dates carries neither, because the same step sets the filtered lists and the flakiness properties together, so any window reaching back past them mixes two schemes — `ingest.py` prefers the filtered lists per build and falls back to the raw ones.

## Layout

```
ews_dashboard/
  schema.sql      tables, views, and the invalidation rule for every cache
  db.py           connections and forward-only migrations
  config.py       thresholds, rule names, and the dated list of EWS deployments
  suites.py       which builders are read, and how each publishes its failure lists
  buildbot.py     EWS's Buildbot API
  results.py      results.webkit.org history, cached, including negative caching
  ingest.py       builds and flakiness verdicts into the database
  analysis/       false_positive, convictions, trend, freshness
  web/            Flask app, links out, SVG chart geometry, templates
scripts/refresh.py
tests/
```

There is no JavaScript, no CDN, no build step and no npm. The chart is server-generated SVG, and
`ews_dashboard/web/static/dashboard.css` is the whole stylesheet, so the pages render from a
checkout with no network.
