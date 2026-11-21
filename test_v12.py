"""Behavioral regressions for AST comparison, lambdas and class data reads."""
import tempfile
import unittest
from pathlib import Path

from impact import Selector
from test_v1 import FILE, patch
from trace_map import Recorder


class StructuralSelectionTests(unittest.TestCase):
    def prepare(self, source, cases, files=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "sqlglot").mkdir()
        path = root / FILE
        path.write_text(source)
        for name, content in (files or {}).items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        namespace = {"__file__": str(path)}
        tests = [{"number": i, "id": "tests/test_structural.py::" + name, "seconds": 0.01}
                 for i, name in enumerate(cases)]
        recorder = Recorder(root)
        recorder.start()
        try:
            exec(compile(source, str(path), "exec"), namespace)
            for test, (name, args, expected) in zip(tests, cases.values()):
                recorder.context = test["id"]
                self.assertEqual(expected, namespace[name](*args))
        finally:
            recorder.stop()
        return Selector(tests, recorder.export([t["id"] for t in tests])), path

    def choose(self, selector, before, after):
        selected, reason = selector.choose(patch(before, after))
        return {t["number"] for t in selected}, reason

    def test_plain_comment_uses_ast_comparison_not_full_fallback(self):
        source = "def value():\n    return 2\n\ndef other():\n    return 7\n"
        selector, _ = self.prepare(source, {"value": ("value", (), 2), "other": ("other", (), 7)})
        selected, reason = self.choose(selector, source, "# ordinary note\n" + source)
        self.assertEqual(set(), selected)
        self.assertIn("ast-equal", reason)

    def test_source_reader_is_retained_for_an_ast_equal_comment(self):
        source = '''from pathlib import Path

def read_source():
    return len(Path(__file__).read_text())

def other():
    return 7
'''
        selector, path = self.prepare(source, {"source": ("read_source", (), len(source)),
                                               "other": ("other", (), 7)})
        changed = "# ordinary note\n" + source
        selected, reason = self.choose(selector, source, changed)
        self.assertEqual({0}, selected)
        path.write_text(changed)
        namespace = {"__file__": str(path)}
        exec(compile(changed, str(path), "exec"), namespace)
        self.assertNotEqual(len(source), namespace["read_source"]())

    def test_hash_inside_a_string_is_a_behavior_change(self):
        source = 'def value():\n    return "# old"\n\ndef other():\n    return 7\n'
        selector, _ = self.prepare(source, {"value": ("value", (), "# old"), "other": ("other", (), 7)})
        selected, reason = self.choose(selector, source, source.replace("# old", "# new"))
        self.assertIn(0, selected)
        self.assertNotIn("ast-equal", reason)

    def test_docstring_change_is_not_skipped(self):
        source = 'def value():\n    """old"""\n    return 2\n\ndef other():\n    return 7\n'
        selector, _ = self.prepare(source, {"value": ("value", (), 2), "other": ("other", (), 7)})
        selected, reason = self.choose(selector, source, source.replace('"""old"""', '"""new"""'))
        self.assertIn(0, selected)
        self.assertNotIn("ast-equal", reason)

    def test_lambda_body_in_class_table_selects_its_caller(self):
        source = '''class Config:
    HANDLERS = {"x": lambda n: n * 2}

def value():
    return Config.HANDLERS["x"](3)

def other():
    return 7
'''
        selector, path = self.prepare(source, {"value": ("value", (), 6), "other": ("other", (), 7)})
        changed = source.replace("n * 2", "n * 3")
        selected, reason = self.choose(selector, source, changed)
        self.assertEqual({0}, selected)
        self.assertIn("lambda-body", reason)
        namespace = {}
        exec(compile(changed, str(path), "exec"), namespace)
        self.assertNotEqual(6, namespace["value"]())

    def test_lambda_default_change_keeps_initialization_fallback(self):
        source = '''class Config:
    CALL = lambda n=2: n * 2

def value():
    return Config.CALL()

def other():
    return 7
'''
        selector, _ = self.prepare(source, {"value": ("value", (), 4), "other": ("other", (), 7)})
        selected, reason = self.choose(selector, source, source.replace("n=2", "n=3"))
        self.assertEqual({0, 1}, selected)
        self.assertTrue(reason.startswith("fallback:"))

    def test_lambda_without_body_line_events_uses_its_binding_readers(self):
        source = '''class Config:
    HANDLERS = {"x": lambda n: n * 2}

def value():
    return Config.HANDLERS["x"](3)

def other():
    return 7
'''
        selector, _ = self.prepare(source, {"value": ("value", (), 6), "other": ("other", (), 7)})
        # Model the observed 3.13 gap even when running this test on 3.12:
        # this line only has a class-creation event, not a lambda-body event.
        from trace_map import GLOBAL
        contexts = selector.mapping["contexts"]
        selector.mapping["files"][FILE]["lines"]["2"] = [contexts.index(GLOBAL)]
        selected, reason = self.choose(selector, source, source.replace("n * 2", "n * 3"))
        self.assertEqual({0}, selected)
        self.assertIn("lambda-binding-readers", reason)

    def test_unbound_inline_lambda_keeps_conservative_fallback(self):
        source = '''def value():
    return (lambda n: n * 2)(3)

def other():
    return 7
'''
        selector, _ = self.prepare(source, {"value": ("value", (), 6), "other": ("other", (), 7)})
        selected, reason = self.choose(selector, source, source.replace("n * 2", "n * 3"))
        self.assertEqual({0, 1}, selected)
        self.assertTrue(reason.startswith("fallback:"))

    def test_literal_class_attribute_uses_runtime_readers(self):
        source = '''class Config:
    VALUE = 2

def value():
    return Config.VALUE

def other():
    return 7
'''
        selector, path = self.prepare(source, {"value": ("value", (), 2), "other": ("other", (), 7)})
        changed = source.replace("VALUE = 2", "VALUE = 3")
        selected, reason = self.choose(selector, source, changed)
        self.assertEqual({0}, selected)
        self.assertIn("class-literal-readers", reason)
        namespace = {}
        exec(compile(changed, str(path), "exec"), namespace)
        self.assertNotEqual(2, namespace["value"]())

    def test_globally_copied_class_attribute_still_runs_everything(self):
        source = '''class Config:
    VALUE = 2

COPIED = Config.VALUE

def value():
    return COPIED

def other():
    return 7
'''
        selector, _ = self.prepare(source, {"value": ("value", (), 2), "other": ("other", (), 7)})
        selected, reason = self.choose(selector, source, source.replace("VALUE = 2", "VALUE = 3"))
        self.assertEqual({0, 1}, selected)
        self.assertTrue(reason.startswith("fallback:"))

    def test_changed_encoding_is_not_assumed_harmless(self):
        source = '# coding: utf-8\ndef value():\n    return 2\n\ndef other():\n    return 7\n'
        selector, _ = self.prepare(source, {"value": ("value", (), 2), "other": ("other", (), 7)})
        selected, reason = self.choose(selector, source, source.replace("utf-8", "latin-1"))
        self.assertEqual({0, 1}, selected)
        self.assertIn("encoding", reason)

    def test_comment_in_one_file_does_not_skip_a_changed_fixture(self):
        source = '''from pathlib import Path

def value():
    with open(Path(__file__).with_name("rate.txt")) as stream:
        return int(stream.read())

def other():
    return 7
'''
        selector, _ = self.prepare(source, {"value": ("value", (), 10), "other": ("other", (), 7)},
                                   files={"sqlglot/rate.txt": "10\n"})
        text = patch(source, "# note\n" + source)
        text += patch("10\n", "20\n", "sqlglot/rate.txt")
        selected, _ = selector.choose(text)
        self.assertEqual({0}, {t["number"] for t in selected})


if __name__ == "__main__":
    unittest.main()
