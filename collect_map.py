"""Run the unchanged pinned sqlglot tests and generate a coverage index.

Usage: python collect_map.py --source /path/to/pinned/sqlglot --output map.json.gz
Requires the organizer's requirements.lock, and intentionally fails closed
when baseline tests fail. This is a build/development command, not runtime.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from trace_map import GLOBAL, PIN, Recorder, save_map


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--test-timeout", type=int, default=300,
                        help="collection-only per-test limit; it does not change the scorer's test costs")
    parser.add_argument("--tests", nargs="+", default=["tests"],
                        help="optional smoke-test node ids; production maps must cover the full suite")
    parser.add_argument("--source-verified-by-build", action="store_true",
                        help="Docker only: prior git stage fetched and checked out the exact pin")
    args = parser.parse_args()
    source = args.source.resolve()
    output = args.output.resolve()
    if args.source_verified_by_build:
        # Docker's source stage checks out a detached HEAD; no tracked files
        # are changed between that stage and collection. No git binary needed.
        if (source / ".git/HEAD").read_text().strip() != PIN:
            parser.error("build source HEAD is not the required detached pin")
    else:
        actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
        if actual != PIN:
            parser.error(f"source must be pinned at {PIN}, got {actual}")
        dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"],
                                        cwd=source, text=True)
        if dirty.strip():
            parser.error("source contains tracked modifications; collect on an unchanged baseline")
    os.chdir(source)
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    sys.path.insert(0, str(source))
    recorder = Recorder(source)
    recorder.start()  # Include pytest collection and sqlglot's first imports.
    try:
        import pytest

        class Plugin:
            test_ids = []

            def pytest_collection_modifyitems(self, items):
                self.test_ids = [item.nodeid for item in items]

            @pytest.hookimpl(hookwrapper=True, tryfirst=True)
            def pytest_runtest_protocol(self, item, nextitem):
                recorder.context = item.nodeid
                try:
                    yield
                finally:
                    recorder.context = GLOBAL

        plugin = Plugin()
        # Serial tests: one active test context, covering setup/call/teardown.
        code = int(pytest.main(["-q", "-p", "pytest_timeout", f"--timeout={args.test_timeout}",
                                "-o", "addopts=", *args.tests], plugins=[plugin]))
    finally:
        recorder.stop()
    if code or not plugin.test_ids:
        raise SystemExit(f"baseline collection/testing failed (exit {code}); map not written")
    mapping = recorder.export(plugin.test_ids, exit_code=code)
    if not mapping["observed_test_ids"]:
        raise SystemExit("no project execution was recorded; refusing to write an empty coverage map")
    save_map(output, mapping)
    print(f"saved {output}: {len(plugin.test_ids)} collected tests", file=sys.stderr)


if __name__ == "__main__":
    main()
