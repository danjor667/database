#!/usr/bin/env python3
"""EXPLAIN ANALYZE harness for the Day 4 queries.

    .venv/bin/python scripts/explain.py              # profile every probe
    .venv/bin/python scripts/explain.py --scale      # add the index-crossover study
    .venv/bin/python scripts/explain.py -k location  # only probes matching a name

Each probe runs `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` several times and
reports the **median**, not the fastest or the mean. A single run measures cache
state as much as the query, and the first run of anything is unrepresentative —
so run 1 is discarded as a warm-up and the median of the rest resists the
occasional outlier from an autovacuum or a checkpoint landing mid-run.

Timings come from Postgres's own `Execution Time`, which excludes client round
trip and result serialisation. On Apple Silicon the container is native
(`imresamu/postgis`), so these are real numbers rather than emulated ones.

A/B probes disable a plan type with `SET LOCAL enable_*` and re-run. That does
not delete the index; it forces the planner to cost the alternative, which is how
you find out what an index is actually worth rather than assuming.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import Database, load_settings  # noqa: E402
from app.availability import AVAILABILITY_SQL, build_params  # noqa: E402

REPEATS = 7

# Planner knobs used by the A/B probes. Disabling all three index paths leaves
# the planner no option but a sequential scan.
NO_INDEX = (
    "SET LOCAL enable_indexscan = off",
    "SET LOCAL enable_bitmapscan = off",
    "SET LOCAL enable_indexonlyscan = off",
)


@dataclass
class Probe:
    name: str
    sql: str
    params: dict | tuple = field(default_factory=dict)
    note: str = ""
    # Statements run before the query inside the same transaction.
    setup: tuple[str, ...] = ()


@dataclass
class Result:
    median_ms: float
    spread_ms: float
    planning_ms: float
    rows: int
    buffers: int
    nodes: list[str]
    indexes: list[str]

    @property
    def scan_summary(self) -> str:
        if self.indexes:
            return ", ".join(sorted(set(self.indexes)))
        seq = [n for n in self.nodes if "Seq Scan" in n]
        return "seq scan" if seq else "—"


def _walk(plan: dict, nodes: list[str], indexes: list[str]) -> None:
    node = plan.get("Node Type", "")
    detail = node
    if plan.get("Relation Name"):
        detail = f"{node} on {plan['Relation Name']}"
    nodes.append(detail)
    if plan.get("Index Name"):
        indexes.append(plan["Index Name"])
    for child in plan.get("Plans", []) or []:
        _walk(child, nodes, indexes)


def run_probe(db: Database, probe: Probe, repeats: int = REPEATS) -> Result:
    samples: list[float] = []
    last: dict = {}

    for _ in range(repeats + 1):  # +1 warm-up, discarded below
        with db.transaction(readonly=True) as cur:
            for statement in probe.setup:
                cur.execute(statement)
            cur.execute(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + probe.sql, probe.params
            )
            row = cur.fetchone()
            last = list(row.values())[0][0]
        samples.append(last["Execution Time"])

    samples = samples[1:]  # drop the warm-up
    nodes: list[str] = []
    indexes: list[str] = []
    _walk(last["Plan"], nodes, indexes)

    return Result(
        median_ms=statistics.median(samples),
        spread_ms=max(samples) - min(samples),
        planning_ms=last.get("Planning Time", 0.0),
        rows=last["Plan"].get("Actual Rows", 0),
        buffers=last["Plan"].get("Shared Hit Blocks", 0)
        + last["Plan"].get("Shared Read Blocks", 0),
        nodes=nodes,
        indexes=indexes,
    )


# -- the probes -------------------------------------------------------------


def build_probes(db: Database) -> list[Probe]:
    slot = datetime.combine(
        date.today() + timedelta(days=7), time(19, 0), tzinfo=timezone.utc
    )
    with db.transaction(readonly=True) as cur:
        cur.execute("SELECT restaurant_id FROM restaurants WHERE name = 'Vero Nord'")
        rid = cur.fetchone()["restaurant_id"]
        cur.execute("SELECT customer_id FROM customers ORDER BY customer_id LIMIT 1")
        cid = cur.fetchone()["customer_id"]

    avail = build_params(restaurant_id=rid, starts_at=slot, party_size=4)

    # Times Square, for the radius and nearest-neighbour probes.
    lon, lat = -73.9855, 40.7580

    return [
        Probe(
            "availability-cte",
            AVAILABILITY_SQL,
            avail,
            "the Day 2 booking read path; the query the lab asks us to optimize",
        ),
        Probe(
            "availability-cte (no index)",
            AVAILABILITY_SQL,
            avail,
            "same query, planner forced onto sequential scans",
            setup=NO_INDEX,
        ),
        Probe(
            "ranking-per-city",
            "SELECT * FROM v_restaurant_rankings",
            {},
            "RANK() OVER (PARTITION BY city_id ORDER BY avg_rating DESC)",
        ),
        Probe(
            "ratings-rollup (current)",
            "SELECT * FROM v_restaurant_ratings",
            {},
            "aggregates reviews first, then joins",
        ),
        Probe(
            "ratings-rollup (original shape)",
            """
            SELECT r.restaurant_id, count(rv.review_id) AS review_count,
                   avg(rv.rating) AS avg_rating, max(upper(res.during)) AS last_visit
              FROM restaurants r
              LEFT JOIN restaurant_tables rt  ON rt.restaurant_id = r.restaurant_id
              LEFT JOIN reservations      res ON res.table_id = rt.table_id
              LEFT JOIN reviews           rv  ON rv.reservation_id = res.reservation_id
             WHERE r.is_active
             GROUP BY r.restaurant_id
            """,
            {},
            "join the whole chain, then GROUP BY — the shape 05_views.sql replaced",
        ),
        Probe(
            "location-radius",
            """
            SELECT restaurant_id, name,
                   round(ST_Distance(location, ST_MakePoint(%(lon)s, %(lat)s)::geography)::numeric, 0) AS metres
              FROM restaurants
             WHERE ST_DWithin(location, ST_MakePoint(%(lon)s, %(lat)s)::geography, %(radius)s)
             ORDER BY metres
            """,
            {"lon": lon, "lat": lat, "radius": 5000},
            "ST_DWithin radius search over the GiST index",
        ),
        Probe(
            "location-knn",
            """
            SELECT restaurant_id, name
              FROM restaurants
             ORDER BY location <-> ST_MakePoint(%(lon)s, %(lat)s)::geography
             LIMIT 5
            """,
            {"lon": lon, "lat": lat},
            "<-> nearest-neighbour ordering",
        ),
        Probe(
            "customer-bookings",
            """
            SELECT reservation_id, during, status FROM reservations
             WHERE customer_id = %(cid)s ORDER BY lower(during) DESC LIMIT 20
            """,
            {"cid": cid},
            "idx_reservations_customer_recent",
        ),
        Probe(
            "slot-demand",
            "SELECT * FROM v_slot_demand WHERE demand_rank <= 3",
            {},
            "window function over an aggregate",
        ),
    ]


# -- the index crossover study ----------------------------------------------

_SCALE_SETUP = """
DROP TABLE IF EXISTS scale_restaurants;
CREATE TABLE scale_restaurants (
    id       integer PRIMARY KEY,
    location geography(Point, 4326) NOT NULL
);
-- 50k points scattered over a ~1000 km box centred on New York. Deterministic:
-- setseed makes the crossover measurement repeatable.
SELECT setseed(0.17);
INSERT INTO scale_restaurants (id, location)
SELECT g,
       ST_MakePoint(-74.0 + (random() - 0.5) * 10.0,
                     40.7 + (random() - 0.5) * 8.0)::geography
  FROM generate_series(1, 50000) AS g;
ANALYZE scale_restaurants;
"""

_SCALE_QUERY = """
SELECT count(*) FROM scale_restaurants
 WHERE ST_DWithin(location, ST_MakePoint(-73.9855, 40.7580)::geography, 5000)
"""

# Same predicate written the way that reads more naturally and cannot use the
# index: the planner has no way to bound the output of ST_Distance with a GiST
# index, so it must evaluate the distance for every row.
_SCALE_QUERY_UNINDEXABLE = """
SELECT count(*) FROM scale_restaurants
 WHERE ST_Distance(location, ST_MakePoint(-73.9855, 40.7580)::geography) <= 5000
"""

# KNN: the index is walked in distance order and stops after LIMIT rows.
_SCALE_QUERY_KNN = """
SELECT id FROM scale_restaurants
 ORDER BY location <-> ST_MakePoint(-73.9855, 40.7580)::geography
 LIMIT 5
"""


def run_scale_study(db: Database) -> None:
    """Show where a GiST index starts to pay, since 8 rows never will.

    The seed has 8 restaurants, which fit in a single page — a sequential scan
    beats any index there, and the planner is right to ignore
    `idx_restaurants_location`. That makes the seed useless for demonstrating
    what the index is for, so this builds a 50k-row table and measures both
    plans on it.
    """
    print("\n" + "=" * 78)
    print("INDEX CROSSOVER: GiST on geography, 50,000 points")
    print("=" * 78)

    with db.transaction() as cur:
        for statement in filter(None, (s.strip() for s in _SCALE_SETUP.split(";"))):
            cur.execute(statement)

    try:
        with_index = run_probe(db, Probe("with gist", _SCALE_QUERY))
        print(f"  no index yet (seq scan):  {with_index.median_ms:8.2f} ms   "
              f"[{with_index.scan_summary}]")

        with db.transaction() as cur:
            cur.execute(
                "CREATE INDEX scale_restaurants_gist ON scale_restaurants USING gist (location)"
            )
            cur.execute("ANALYZE scale_restaurants")

        indexed = run_probe(db, Probe("with gist", _SCALE_QUERY))
        forced_seq = run_probe(db, Probe("forced seq", _SCALE_QUERY, setup=NO_INDEX))
        unindexable = run_probe(db, Probe("st_distance", _SCALE_QUERY_UNINDEXABLE))
        knn = run_probe(db, Probe("knn", _SCALE_QUERY_KNN))

        print(f"  GiST index scan:          {indexed.median_ms:8.2f} ms   "
              f"[{indexed.scan_summary}]")
        print(f"  forced sequential scan:   {forced_seq.median_ms:8.2f} ms   "
              f"[{forced_seq.scan_summary}]")
        if indexed.median_ms > 0:
            print(f"  speedup: {forced_seq.median_ms / indexed.median_ms:.0f}x  "
                  f"({forced_seq.buffers} buffers -> {indexed.buffers})")

        print(f"\n  ST_Distance(...) <= r:    {unindexable.median_ms:8.2f} ms   "
              f"[{unindexable.scan_summary}]")
        print("    ^ same answer, index unusable: the planner cannot bound a")
        print("      function's output with GiST, so every row is measured.")
        print(f"\n  KNN <-> ORDER BY LIMIT 5: {knn.median_ms:8.2f} ms   "
              f"[{knn.scan_summary}]")
    finally:
        with db.transaction() as cur:
            cur.execute("DROP TABLE IF EXISTS scale_restaurants")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-k", "--filter", help="only probes whose name contains this")
    parser.add_argument("-n", "--repeats", type=int, default=REPEATS)
    parser.add_argument("--scale", action="store_true", help="run the crossover study")
    args = parser.parse_args()

    settings = load_settings()
    with Database.from_settings(settings) as db:
        with db.transaction(readonly=True) as cur:
            cur.execute("SELECT version()")
            version = cur.fetchone()["version"].split(" on ")[0]
            cur.execute("SELECT count(*) AS n FROM reservations")
            reservations = cur.fetchone()["n"]

        print(f"{version}   |   {reservations} reservations   |   "
              f"median of {args.repeats} runs after one warm-up\n")

        probes = build_probes(db)
        if args.filter:
            probes = [p for p in probes if args.filter.lower() in p.name.lower()]
            if not probes and not args.scale:
                print(f"no probe matches {args.filter!r}")
                return 1

        if probes:
            _run_table(db, probes, args.repeats)

        if args.scale:
            run_scale_study(db)

    return 0


def _run_table(db: Database, probes: list[Probe], repeats: int) -> None:
        width = max(len(p.name) for p in probes)
        print(f"{'probe'.ljust(width)}  {'exec':>9}  {'±':>7}  {'plan':>8}  "
              f"{'rows':>6}  {'bufs':>6}  access path")
        print("-" * (width + 56))

        results: dict[str, Result] = {}
        for probe in probes:
            result = run_probe(db, probe, repeats)
            results[probe.name] = result
            print(f"{probe.name.ljust(width)}  {result.median_ms:8.3f}ms  "
                  f"{result.spread_ms:6.3f}  {result.planning_ms:7.3f}ms  "
                  f"{result.rows:6d}  {result.buffers:6d}  {result.scan_summary}")

        print()
        for probe in probes:
            if probe.note:
                print(f"  {probe.name}: {probe.note}")

        _report_pairs(results)


def _report_pairs(results: dict[str, Result]) -> None:
    """Print the A/B comparisons, when both halves were actually run."""
    pairs = [
        ("availability-cte", "availability-cte (no index)", "index vs sequential"),
        ("ratings-rollup (current)", "ratings-rollup (original shape)",
         "aggregate-first vs join-then-group"),
    ]
    shown = False
    for fast, slow, label in pairs:
        if fast in results and slow in results:
            if not shown:
                print("\ncomparisons")
                shown = True
            a, b = results[fast], results[slow]
            ratio = b.median_ms / a.median_ms if a.median_ms else float("nan")
            print(f"  {label}: {a.median_ms:.3f}ms vs {b.median_ms:.3f}ms  "
                  f"({ratio:.2f}x, {b.buffers - a.buffers:+d} buffers)")


if __name__ == "__main__":
    raise SystemExit(main())