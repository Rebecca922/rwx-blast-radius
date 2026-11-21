"""Coverage-guided selection with conservative fallbacks and LPT scheduling.

Runtime reads only patches and test metadata, never sample answers.
"""

import json
import sys
import argparse
import math
from pathlib import Path

from impact import Selector
from trace_map import load_map

DATA = Path("/data")


def split(tests: list[dict], worker_count: int) -> list[list[int]]:
    """Longest test first, each onto the worker with the least to do so far."""
    workers: list[list[int]] = [[] for _ in range(worker_count)]
    loads = [0] * worker_count
    for test in sorted(tests, key=lambda test: test["seconds"], reverse=True):
        lightest = loads.index(min(loads))
        workers[lightest].append(test["number"])
        loads[lightest] += math.floor(test["seconds"] * 1000 + 0.5)
    return [worker for worker in workers if worker]


def best_split(tests: list[dict]) -> list[list[int]]:
    """Compare LPT schedules for all permitted worker counts.

    LPT is a fast approximation, not an exact partition optimizer.
    Cost comparisons use scorer-compatible integer milliseconds.
    """
    if not tests:
        return []
    durations = {t["number"]: math.floor(t["seconds"] * 1000 + 0.5) for t in tests}
    candidates = [split(tests, k) for k in range(1, min(8, len(tests)) + 1)]
    return min(candidates, key=lambda ws: (
        max(sum(durations[n] for n in w) for w in ws) + 2000 * len(ws), len(ws)
    ))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument("--map", type=Path, default=Path(__file__).with_name("map.json.gz"))
    parser.add_argument("--all", action="store_true", help="development control: select all tests")
    args = parser.parse_args()
    tests = json.loads((args.data / "tests.json").read_text())["tests"]
    mapping = None
    if not args.all:
        try:
            mapping = load_map(args.map)
        except (OSError, ValueError) as error:
            print(f"coverage map unavailable: {error}", file=sys.stderr)
    selector = Selector(tests, mapping)
    schedules = {}
    for patch in sorted((args.data / "commits").glob("*.patch")):
        selected, reason = selector.choose(patch.read_text())
        key = tuple(t["number"] for t in selected)
        if key not in schedules:
            schedules[key] = best_split(selected)
        workers = schedules[key]
        print(json.dumps({"commit": patch.stem, "workers": workers}))
        print(f"{patch.stem}: {len(selected)}/{len(tests)} tests, {len(workers)} workers; {reason}", file=sys.stderr)


if __name__ == "__main__":
    main()
