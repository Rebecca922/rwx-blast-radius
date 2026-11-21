"""Complete unchanged-baseline capture with task-level child attribution."""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from trace_map import GLOBAL, PIN, save_map
from trace_v2 import CompleteRecorder
from worker_trace import ProcessCapture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tests", nargs="+", default=["tests"])
    parser.add_argument("--test-timeout", type=int, default=300)
    parser.add_argument("--source-verified-by-build", action="store_true")
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    if args.source_verified_by_build:
        if (source / ".git/HEAD").read_text().strip() != PIN:
            parser.error("baseline HEAD does not match the required pinned commit")
    else:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
        dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=source, text=True)
        if head != PIN or dirty.strip():
            parser.error("baseline must be clean and pinned")
    output.parent.mkdir(parents=True, exist_ok=True)
    worker_folder = Path(tempfile.mkdtemp(prefix="worker-capture-", dir=output.parent))
    os.chdir(source)
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    sys.path.insert(0, str(source))
    recorder = CompleteRecorder(source)
    capture = ProcessCapture(recorder, worker_folder)
    start = time.monotonic()
    recorder.start()
    capture.install()
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
        code = int(pytest.main(["-q", "-p", "pytest_timeout", f"--timeout={args.test_timeout}",
                               "-o", "addopts=", *args.tests], plugins=[plugin]))
        capture.finish()
    finally:
        capture.restore()
        recorder.stop()
    if code or not plugin.test_ids:
        raise SystemExit(f"baseline failed (exit {code}); no replacement map written")
    proof = capture.merge()
    mapping = recorder.export(plugin.test_ids, exit_code=code)
    mapping["process_capture"] = proof
    mapping["collector"] = "task-attributed-blocks-v1"
    if not mapping["observed_test_ids"]:
        raise SystemExit("no project execution observed; no replacement map written")
    save_map(output, mapping)
    shutil.rmtree(worker_folder)
    print(f"Complete baseline map saved: {len(plugin.test_ids)} tests, "
          f"{proof['completed_tasks']}/{proof['expected_tasks']} child tasks, "
          f"{time.monotonic() - start:.1f}s", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
