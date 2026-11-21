"""Mechanism tests on a tiny synthetic project, not sqlglot accuracy claims."""
import difflib
import json
import tempfile
import unittest
import subprocess
import sys
from pathlib import Path

from impact import Selector, apply_to_source, parse_patch
from selector import best_split
from trace_map import GLOBAL, Recorder, load_map, save_map

SOURCE = '''from pathlib import Path

def double(x):
    return x * 2

def sign(x):
    if x > 0:
        return "positive"
    return "nonpositive"

def load_rate():
    return int(Path(__file__).with_name("rate.txt").read_text())
'''
FILE = "sqlglot/core.py"


def patch(before, after, path=FILE):
    return f"diff --git a/{path} b/{path}\n" + "".join(difflib.unified_diff(
        before.splitlines(True), after.splitlines(True), fromfile="a/" + path, tofile="b/" + path))


def run_case(name, module):
    if name == "double_two":
        assert module["double"](2) == 4
    elif name == "double_zero":
        assert module["double"](0) == 0
    elif name == "positive":
        assert module["sign"](1) == "positive"
    elif name == "negative":
        assert module["sign"](-1) == "nonpositive"
    elif name == "rate":
        assert module["load_rate"]() == 10


class CoverageFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "sqlglot").mkdir()
        (self.root / "tests").mkdir()
        self.source_file = self.root / FILE
        self.source_file.write_text(SOURCE)
        (self.root / "sqlglot/rate.txt").write_text("10\n")
        self.names = ["double_two", "double_zero", "positive", "negative", "rate"]
        self.tests = [{"number": i, "id": "tests/test_demo.py::" + name,
                       "file": "tests/test_demo.py", "seconds": 0.01}
                      for i, name in enumerate(self.names)]
        namespace = {"__file__": str(self.source_file)}
        recorder = Recorder(self.root)
        recorder.start()
        try:
            exec(compile(SOURCE, str(self.source_file), "exec"), namespace)
            for test, name in zip(self.tests, self.names):
                recorder.context = test["id"]
                run_case(name, namespace)
        finally:
            recorder.stop()
        self.mapping = recorder.export([t["id"] for t in self.tests])
        self.selector = Selector(self.tests, self.mapping)

    def picked(self, text, selector=None):
        selected, reason = (selector or self.selector).choose(text)
        return {t["number"] for t in selected}, reason

    def test_arithmetic_change_selects_both_callers_and_not_unrelated_tests(self):
        selected, reason = self.picked(patch(SOURCE, SOURCE.replace("x * 2", "x * 3")))
        self.assertEqual({0, 1}, selected)
        self.assertEqual("coverage", reason)

    def test_condition_change_selects_both_branches(self):
        selected, _ = self.picked(patch(SOURCE, SOURCE.replace("x > 0", "x < 0")))
        self.assertEqual({2, 3}, selected)

    def test_fixture_read_is_attributed_to_its_test(self):
        selected, _ = self.picked(patch("10\n", "20\n", "sqlglot/rate.txt"))
        self.assertEqual({4}, selected)

    def test_insertion_selects_enclosing_function(self):
        selected, _ = self.picked(patch(SOURCE, SOURCE.replace("    return x * 2", "    x += 1\n    return x * 2")))
        self.assertTrue({0, 1} <= selected)

    def test_multiple_files_union_their_tests(self):
        text = patch(SOURCE, SOURCE.replace("x * 2", "x * 3"))
        text += patch("10\n", "20\n", "sqlglot/rate.txt")
        self.assertEqual({0, 1, 4}, self.picked(text)[0])

    def test_syntax_error_selects_everything(self):
        selected, reason = self.picked(patch(SOURCE, SOURCE.replace("return x * 2", "return (")))
        self.assertEqual(set(range(5)), selected)
        self.assertTrue(reason.startswith("fallback:"))

    def test_module_import_change_selects_everything(self):
        selected, _ = self.picked(patch(SOURCE, SOURCE.replace("from pathlib import Path", "from pathlib import Missing")))
        self.assertEqual(set(range(5)), selected)

    def test_incompatible_test_inventory_falls_back(self):
        tests = self.tests + [{"id": "tests/new.py::test_new", "number": 99}]
        selected, _ = Selector(tests, self.mapping).choose(patch(SOURCE, SOURCE.replace("x * 2", "x * 3")))
        self.assertEqual(6, len(selected))

    def test_runtime_test_numbers_can_change(self):
        renumbered = [dict(t, number=t["number"] + 100) for t in self.tests]
        selected, _ = self.picked(patch(SOURCE, SOURCE.replace("x * 2", "x * 3")), Selector(renumbered, self.mapping))
        self.assertEqual({100, 101}, selected)

    def test_wrong_baseline_context_and_malformed_hunk_fall_back(self):
        text = patch(SOURCE, SOURCE.replace("x * 2", "x * 3"))
        for bad in [text.replace("-    return x * 2", "-    return x * 9"), text.replace("@@ -", "@@ -999")]:
            self.assertEqual(set(range(5)), self.picked(bad)[0])

    def test_map_roundtrip(self):
        path = self.root / "map.json.gz"
        save_map(path, self.mapping)
        self.assertEqual(self.mapping, load_map(path))

    def test_command_line_output_passes_official_scorer(self):
        from score import evaluate_run

        data = self.root / "data"
        (data / "commits").mkdir(parents=True)
        (data / "tests.json").write_text(json.dumps({"tests": self.tests}))
        (data / "commits/demo.patch").write_text(patch(SOURCE, SOURCE.replace("x * 2", "x * 3")))
        map_path = self.root / "map.json.gz"
        save_map(map_path, self.mapping)
        run = subprocess.run([sys.executable, "-B", str(Path(__file__).with_name("selector.py")),
                              "--data", str(data), "--map", str(map_path)],
                             capture_output=True, check=True)
        answer = json.loads(run.stdout)
        self.assertEqual({0, 1}, {n for w in answer["workers"] for n in w})
        result = evaluate_run(run.stdout, self.tests, {"demo": {"failing": [0]}})
        self.assertTrue(result["ok"])
        self.assertEqual([], result["missed_commits"])

    def test_real_mutations_do_not_escape_selection(self):
        # Apply changes, run the same assertions on the changed program, then
        # verify every actually failing test was selected. Answers are measured,
        # not used to build the map or choose the candidate tests.
        edits = [("x * 2", "x * 3"), ("x * 2", "x + 2"),
                 ("x > 0", "x < 0"), ('return "positive"', 'return "bad"'),
                 ('return "nonpositive"', 'return "bad"')]
        for before, after in edits:
            with self.subTest(edit=(before, after)):
                changed = SOURCE.replace(before, after)
                selected, _ = self.picked(patch(SOURCE, changed))
                module = {"__file__": str(self.source_file)}
                exec(compile(changed, str(self.source_file), "exec"), module)
                failing = set()
                for i, name in enumerate(self.names):
                    try:
                        run_case(name, module)
                    except Exception:
                        failing.add(i)
                self.assertTrue(failing)
                self.assertTrue(failing <= selected, (failing, selected))


class PathAliasTests(unittest.TestCase):
    def test_symlinked_project_records_code_and_fixture_reads(self):
        # macOS commonly exposes /var/folders/... as an alias of
        # /private/var/folders/.... Reproduce the same mismatch on Linux.
        with tempfile.TemporaryDirectory() as tmp:
            physical = Path(tmp) / "physical"
            (physical / "sqlglot").mkdir(parents=True)
            (physical / "sqlglot/rate.txt").write_text("10\n")
            (physical / FILE).write_text(SOURCE)
            alias = Path(tmp) / "alias"
            alias.symlink_to(physical, target_is_directory=True)
            recorder = Recorder(alias)
            namespace = {"__file__": str(alias / FILE)}
            recorder.start()
            try:
                exec(compile(SOURCE, str(alias / FILE), "exec"), namespace)
                recorder.context = "test_alias"
                self.assertEqual(4, namespace["double"](2))
                self.assertEqual(10, namespace["load_rate"]())
            finally:
                recorder.stop()
            self.assertIn("test_alias", recorder.lines[FILE][4])
            self.assertIn("test_alias", recorder.reads["sqlglot/rate.txt"])

    def test_outside_symlink_is_not_treated_as_project_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            outside = Path(tmp) / "outside.py"
            outside.write_text("x = 1\n")
            (root / "escape.py").symlink_to(outside)
            self.assertIsNone(Recorder(root).relative(root / "escape.py"))

    def test_empty_collection_never_authorizes_empty_test_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sqlglot").mkdir()
            (root / FILE).write_text(SOURCE)
            tests = [{"id": "test_a", "number": 0, "seconds": 0.01}]
            mapping = Recorder(root).export(["test_a"])
            selected, reason = Selector(tests, mapping).choose(
                patch(SOURCE, SOURCE.replace("x * 2", "x * 3")))
            self.assertEqual(tests, selected)
            self.assertTrue(reason.startswith("fallback:"))


class RecordingCostTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(sys, "monitoring"), "optimized monitoring requires Python 3.12+")
    def test_hot_loop_records_each_test_without_repeating_every_iteration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sqlglot").mkdir()
            path = root / "sqlglot/hot.py"
            source = "def hot(n):\n    total = 0\n    for i in range(n):\n        total += i\n    return total\n"
            path.write_text(source)

            class Counted(Recorder):
                hits = 0

                def line(self, code, lineno):
                    if code.co_name == "hot":
                        self.hits += 1
                    return super().line(code, lineno)

            recorder = Counted(root)
            namespace = {}
            exec(compile(source, str(path), "exec"), namespace)
            recorder.start()
            try:
                for name in ("first_test", "second_test"):
                    recorder.context = name
                    self.assertEqual(4999950000, namespace["hot"](100000))
            finally:
                recorder.stop()
            # The old implementation generated roughly 400,000 callbacks.
            self.assertLess(recorder.hits, 30)
            self.assertEqual({"first_test", "second_test"}, recorder.lines["sqlglot/hot.py"][4])

    @unittest.skipUnless(hasattr(sys, "monitoring"), "optimized monitoring requires Python 3.12+")
    def test_optimized_and_repeated_recording_produce_identical_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sqlglot").mkdir()
            path = root / FILE
            path.write_text(SOURCE)
            (root / "sqlglot/rate.txt").write_text("10\n")

            class Repeated(Recorder):
                def line(self, code, lineno):
                    super().line(code, lineno)
                    return None

            maps = []
            for cls in (Repeated, Recorder):
                recorder = cls(root)
                namespace = {"__file__": str(path)}
                recorder.start()
                try:
                    exec(compile(SOURCE, str(path), "exec"), namespace)
                    for context in ("a", "b"):
                        recorder.context = context
                        for _ in range(10):
                            namespace["double"](2)
                            namespace["sign"](1)
                            namespace["sign"](-1)
                        namespace["load_rate"]()
                finally:
                    recorder.stop()
                maps.append(recorder.export(["a", "b"]))
            self.assertEqual(maps[0], maps[1])


if __name__ == "__main__":
    unittest.main()
