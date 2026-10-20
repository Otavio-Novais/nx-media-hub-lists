#!/usr/bin/env python3
"""Picks STARTUP_LIMIT / MIN_SPEED for the runner so that its verdict predicts what plays in Brazil.

    python3 calibrate.py <metrics.json from the workflow> <metrics.json from a run in Brazil>

The Brazil run is the ground truth (check.py's own criteria: starts within 10 s, downloads faster than it
plays). For each pair of runner limits it counts channels that would be wrongly hidden (play in Brazil) and
wrongly kept (do not play in Brazil).
"""

import json
import sys

STARTUPS = [3, 4, 5, 6, 7, 8, 10]
SPEEDS = [1.0, 1.5, 2.0, 2.5, 3.0, 4.0]


def predicted_ok(m, startup_limit, min_speed):
    if m.get("startup") is None:
        return m["state"] == "ok"  # direct stream (ok) or hard failure (DNS, 404, 403...)
    return m["startup"] <= startup_limit and (m.get("speed") is None or m["speed"] >= min_speed)


def main(runner_path, brazil_path):
    runner, brazil = json.load(open(runner_path)), json.load(open(brazil_path))
    urls = [u for u in runner if u in brazil]
    plays = {u for u in urls if brazil[u]["state"] == "ok"}
    print(f"{len(urls)} channels in both runs; {len(plays)} play in Brazil\n")
    print("startup  speed   wrongly hidden   wrongly kept   kept")
    rows = []
    for t in STARTUPS:
        for s in SPEEDS:
            ok = {u for u in urls if predicted_ok(runner[u], t, s)}
            rows.append((t, s, len(plays - ok), len(ok - plays), len(ok)))
    for t, s, hidden, kept_bad, kept in rows:
        print(f"{t:5} s  {s:4.1f}x   {hidden:6} ({hidden / max(len(plays), 1):4.0%})   {kept_bad:6} ({kept_bad / max(kept, 1):4.0%})   {kept}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
