"""Regressions for real untraced subprocess and spawned-pool execution."""
import json
import runpy
import tempfile
import unittest
from pathlib import Path

from impact import Selector
from test_v1 import SOURCE, FILE, patch
from trace_map import Recorder


def child_check(path):
    """Picklable worker for the actual spawned ProcessPoolExecutor test."""
    return runpy.run_path(path)["double"](2)


class SubprocessCoverageTests(unittest.TestCase):
    def scenario(self, mode):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "sqlglot").mkdir()
        (root / "tests").mkdir()
        core = root / FILE
        core.write_text(SOURCE)
        rate = root / "tests/rate.txt"
        rate.write_text("10\n")
        if mode == "pool":
            test_source = '''from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from test_v11 import child_check
from pathlib import Path

def test_remote(path):
    with ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn")) as pool:
        assert pool.submit(child_check, str(path)).result(timeout=15) == 4
'''
        else:
            head = "import subprocess\n" if mode == "subprocess" else "from subprocess import run as launch\n"
            call = "subprocess.run" if mode == "subprocess" else "launch"
            test_source = head + '''import sys
from pathlib import Path

def test_remote(path):
    script = "import runpy; m = runpy.run_path(%r); assert m['double'](2) == 4" % str(path)
    result = ''' + call + '''([sys.executable, "-c", script], capture_output=True)
    assert result.returncode == 0
'''
        test_source += '''
def test_local(path):
    assert int(Path(path).read_text()) == 10
'''
        path = root / "tests/test_remote.py"
        path.write_text(test_source)
        tests = [{"number": 0, "id": "tests/test_remote.py::test_remote", "seconds": 0.1},
                 {"number": 1, "id": "tests/test_remote.py::test_local", "seconds": 0.1}]
        namespace = {}
        recorder = Recorder(root)
        recorder.start()
        try:
            exec(compile(test_source, str(path), "exec"), namespace)
            recorder.context = tests[0]["id"]
            namespace["test_remote"](core)
            recorder.context = tests[1]["id"]
            namespace["test_local"](rate)
        finally:
            recorder.stop()
        mapping = recorder.export([t["id"] for t in tests])
        # Prove the actual gap: the parent map never saw double()'s body.
        self.assertEqual({}, mapping["files"][FILE]["lines"])
        return core, namespace, tests, mapping

    def check_remote_mutation(self, mode):
        core, namespace, tests, mapping = self.scenario(mode)
        selector = Selector(tests, mapping)
        self.assertEqual({tests[0]["id"]}, selector.dispatch_tests)
        changed = SOURCE.replace("x * 2", "x * 3")
        selected, reason = selector.choose(patch(SOURCE, changed))
        self.assertEqual([0], [t["number"] for t in selected])
        self.assertEqual("coverage+dispatch-safety", reason)
        core.write_text(changed)
        with self.assertRaises(AssertionError):
            namespace["test_remote"](core)

    def test_real_subprocess_failure_is_selected_despite_missing_child_lines(self):
        self.check_remote_mutation("subprocess")

    def test_imported_dispatch_alias_is_detected(self):
        self.check_remote_mutation("alias")

    def test_real_spawned_process_pool_failure_is_selected(self):
        self.check_remote_mutation("pool")

    def test_fixture_only_patch_keeps_its_recorded_reader(self):
        core, namespace, tests, mapping = self.scenario("subprocess")
        selected, reason = Selector(tests, mapping).choose(patch("10\n", "20\n", "tests/rate.txt"))
        self.assertEqual([1], [t["number"] for t in selected])
        self.assertEqual("coverage", reason)


if __name__ == "__main__":
    unittest.main()
