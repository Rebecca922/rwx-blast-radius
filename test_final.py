"""Final-attempt tests: actual spawn, reused workers, failures and source reads."""
import gzip
import importlib
import json
import multiprocessing
import sys
import tempfile
import unittest
import uuid
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from impact import Selector, untraced_dispatch_tests
from test_v1 import patch
from trace_map import GLOBAL
from trace_v2 import CompleteRecorder
from worker_trace import ProcessCapture


class CompleteCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "sqlglot").mkdir()
        self.module_name = "payload_" + uuid.uuid4().hex
        self.path = self.root / "sqlglot" / (self.module_name + ".py")
        self.source = '''class Payload:
    def __init__(self, value):
        self.value = value
    def __getstate__(self):
        return self.value
    def __setstate__(self, state):
        self.value = state

def twice(payload):
    return payload.value * 2

def explode(payload):
    raise ValueError("expected worker exception")

def never_called():
    return -1

def echo(payload):
    return payload
'''
        self.path.write_text(self.source)
        sys.path.insert(0, str(self.path.parent))
        self.addCleanup(lambda: sys.path.remove(str(self.path.parent)))
        self.addCleanup(lambda: sys.modules.pop(self.module_name, None))
        self.module = importlib.import_module(self.module_name)

    def capture(self, contexts=("test_a", "test_b"), worker_exception=False, echo=False):
        recorder = CompleteRecorder(self.root)
        folder = self.root / "workers"
        capture = ProcessCapture(recorder, folder)
        recorder.start()
        capture.install()
        try:
            with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as pool:
                for name in contexts:
                    recorder.context = name
                    payload = self.module.Payload(3)
                    if worker_exception:
                        with self.assertRaisesRegex(ValueError, "expected worker exception"):
                            pool.submit(self.module.explode, payload).result(timeout=20)
                    elif echo:
                        self.assertEqual(3, pool.submit(self.module.echo, payload).result(timeout=20).value)
                    else:
                        self.assertEqual(6, pool.submit(self.module.twice, payload).result(timeout=20))
            capture.finish()
        finally:
            capture.restore()
            recorder.stop()
        proof = capture.merge()
        mapping = recorder.export(list(contexts))
        mapping["process_capture"] = proof
        return recorder, mapping, capture

    def test_spawned_tasks_keep_separate_contexts_in_one_reused_worker(self):
        recorder, mapping, capture = self.capture()
        entry = mapping["files"]["sqlglot/" + self.path.name]
        contexts = mapping["contexts"]
        # twice() entry and its body must be attributed to both submitted tests.
        self.assertEqual({"test_a", "test_b"}, {contexts[n] for n in entry["entries"]["9"]})
        self.assertEqual({"test_a", "test_b"}, {contexts[n] for n in entry["lines"]["10"]})
        self.assertEqual(2, mapping["process_capture"]["completed_tasks"])
        self.assertEqual(1, mapping["process_capture"]["worker_files"])

    def test_task_argument_deserialization_is_inside_its_test_context(self):
        recorder, mapping, capture = self.capture()
        entry = mapping["files"]["sqlglot/" + self.path.name]
        contexts = mapping["contexts"]
        self.assertEqual({"test_a", "test_b"}, {contexts[n] for n in entry["lines"]["7"]})

    def test_child_result_serialization_keeps_its_task_context(self):
        recorder, mapping, capture = self.capture(echo=True)
        worker_file = next(capture.folder.glob("worker-*.json.gz"))
        with gzip.open(worker_file, "rt") as stream:
            worker = json.load(stream)
        self.assertEqual({"test_a", "test_b"}, set(worker["lines"]["sqlglot/" + self.path.name]["5"]))

    def test_worker_exception_keeps_original_exception_and_coverage(self):
        recorder, mapping, capture = self.capture(contexts=("test_a",), worker_exception=True)
        self.assertEqual(1, mapping["process_capture"]["completed_tasks"])
        entry = mapping["files"]["sqlglot/" + self.path.name]
        self.assertTrue(entry["lines"]["13"])

    def test_missing_worker_output_is_detected(self):
        recorder, mapping, capture = self.capture()
        for path in capture.folder.glob("worker-*.json.gz"):
            path.unlink()
        with self.assertRaisesRegex(RuntimeError, "incomplete child coverage"):
            capture.merge()

    def test_source_read_is_recorded_but_import_reads_are_not(self):
        source = '''from pathlib import Path
def read_source():
    return Path(__file__).read_text()
'''
        self.path.write_text(source)
        namespace = {"__file__": str(self.path)}
        recorder = CompleteRecorder(self.root)
        recorder.start()
        try:
            exec(compile(source, str(self.path), "exec"), namespace)
            recorder.context = "source_test"
            self.assertEqual(source, namespace["read_source"]())
        finally:
            recorder.stop()
        self.assertEqual({"source_test"}, recorder.reads["sqlglot/" + self.path.name])

    def test_import_loader_source_read_is_excluded(self):
        sys.modules.pop(self.module_name, None)
        recorder = CompleteRecorder(self.root)
        recorder.start()
        try:
            importlib.import_module(self.module_name)
        finally:
            recorder.stop()
        self.assertNotIn("sqlglot/" + self.path.name, recorder.reads)

    def test_function_block_selection_covers_a_previously_untaken_line(self):
        source = '''def choose(x):
    if x > 0:
        return x
    return 0
'''
        self.path.write_text(source)
        namespace = {}
        recorder = CompleteRecorder(self.root)
        recorder.start()
        try:
            exec(compile(source, str(self.path), "exec"), namespace)
            recorder.context = "test_positive"
            self.assertEqual(1, namespace["choose"](1))
        finally:
            recorder.stop()
        tests = [{"id": "test_positive", "number": 0, "seconds": 0.01}]
        mapping = recorder.export(["test_positive"])
        selected, _ = Selector(tests, mapping).choose(patch(source, source.replace("return 0", "return -1"),
                                                          "sqlglot/" + self.path.name))
        self.assertEqual([0], [t["number"] for t in selected])

    def test_only_certified_dispatch_sites_lose_blanket_protection(self):
        # A test can use both a captured pool and an uncaptured subprocess.
        # Handling its submit call must not erase its subprocess protection.
        source = "import subprocess\nfrom concurrent.futures import ProcessPoolExecutor\ndef f(pool):\n    pool.submit(fn)\n    subprocess.run(['python'])\n"
        mapping = {"contexts": [GLOBAL, "a", "b"], "files": {
            "tests/test_process.py": {"source": source, "lines": {"4": [1, 2], "5": [1]}}
        }, "process_capture": {"version": 1, "complete": True, "expected_tasks": 2, "completed_tasks": 2,
             "handled_sites": {"tests/test_process.py": {"4": ["a", "b"]}}}}
        self.assertEqual({"a"}, untraced_dispatch_tests(mapping, {"a", "b"}))
        mapping["process_capture"]["complete"] = False
        self.assertEqual({"a", "b"}, untraced_dispatch_tests(mapping, {"a", "b"}))

    def test_multiline_default_that_breaks_import_selects_all_tests(self):
        source = "def choose(\n    x=2,\n):\n    return x\n\ndef other():\n    return 7\n"
        self.path.write_text(source)
        namespace = {}
        recorder = CompleteRecorder(self.root)
        recorder.start()
        try:
            exec(compile(source, str(self.path), "exec"), namespace)
            recorder.context = "a"
            self.assertEqual(2, namespace["choose"]())
            recorder.context = "b"
            self.assertEqual(7, namespace["other"]())
        finally:
            recorder.stop()
        tests = [{"id": "a", "number": 0}, {"id": "b", "number": 1}]
        mapping = recorder.export(["a", "b"])
        changed = source.replace("x=2", "x=missing_name")
        with self.assertRaises(NameError):
            exec(compile(changed, str(self.path), "exec"), {})
        selected, reason = Selector(tests, mapping).choose(patch(source, changed, "sqlglot/" + self.path.name))
        self.assertEqual({0, 1}, {t["number"] for t in selected})
        self.assertIn("signature/default", reason)

    def test_metaclass_copy_is_preserved_by_initialization_fallback(self):
        source = '''class Meta(type):
    def __new__(mcls, name, bases, namespace):
        namespace["COPIED"] = namespace["VALUE"]
        return super().__new__(mcls, name, bases, namespace)

class Config(metaclass=Meta):
    VALUE = 2

def direct():
    return Config.VALUE

def copied():
    return Config.COPIED

def other():
    return 7
'''
        self.path.write_text(source)
        namespace = {}
        recorder = CompleteRecorder(self.root)
        recorder.start()
        try:
            exec(compile(source, str(self.path), "exec"), namespace)
            for name, expected in (("direct", 2), ("copied", 2), ("other", 7)):
                recorder.context = name
                self.assertEqual(expected, namespace[name]())
        finally:
            recorder.stop()
        names = ["direct", "copied", "other"]
        tests = [{"id": n, "number": i} for i, n in enumerate(names)]
        mapping = recorder.export(names)
        changed = source.replace("VALUE = 2", "VALUE = 3")
        selected, reason = Selector(tests, mapping).choose(patch(source, changed, "sqlglot/" + self.path.name))
        changed_namespace = {}
        exec(compile(changed, str(self.path), "exec"), changed_namespace)
        self.assertEqual(3, changed_namespace["copied"]())
        self.assertTrue({0, 1} <= {t["number"] for t in selected})
        self.assertTrue(reason.startswith("fallback:"))


if __name__ == "__main__":
    unittest.main()
