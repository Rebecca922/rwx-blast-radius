"""Patch parsing and conservative baseline-coverage selection.

No patch hashes, sample identifiers, edit-to-failure tables or labels.
Unknowns return all tests. Coverage itself is evidence, not a soundness proof.
"""
from __future__ import annotations

import ast
import io
import re
import shlex
import tokenize
from dataclasses import dataclass, field

from trace_map import GLOBAL, PIN

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

CONCURRENCY_MODULES = ("concurrent", "multiprocessing", "subprocess", "threading", "_thread")
DISPATCH_METHODS = {
    "submit", "map", "starmap", "apply", "apply_async", "map_async",
    "imap", "imap_unordered", "start", "run", "call", "check_call",
    "check_output", "Popen", "start_new_thread",
}

SOURCE_OBSERVERS = {
    "getsource", "getsourcelines", "get_source", "getlines", "getline",
    "read_text", "read_bytes", "__code__", "co_firstlineno", "co_linetable",
    "co_lines", "co_positions", "f_lineno", "tb_lineno", "format_exc",
    "format_exception", "extract_tb", "_getframe", "currentframe", "stack",
}


def source_observer_tests(mapping, all_test_ids):
    """Keep baseline observers of source/code positions even for AST-equal edits.

    Not a proof against arbitrary reflection or external programs. This is
    an explicit additional dependency layer beyond executed business code.
    """
    result = set()
    contexts = mapping["contexts"]
    for entry in mapping["files"].values():
        source = entry.get("source", "")
        if not any(name in source for name in SOURCE_OBSERVERS):
            continue
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Attribute) and node.attr in SOURCE_OBSERVERS:
                lo, hi = node.lineno, node.end_lineno
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                  and node.func.id in SOURCE_OBSERVERS):
                lo, hi = node.lineno, node.end_lineno
            else:
                continue
            for n in range(lo, hi + 1):
                result.update(contexts[i] for i in entry["lines"].get(str(n), []))
    return set(all_test_ids) if GLOBAL in result else result & all_test_ids


def encoding_of(source):
    return tokenize.detect_encoding(io.BytesIO(source.encode("utf-8")).readline)[0]


def same_ast(before, after):
    # Keep type comments/directives in the comparison. Strings containing #
    # are AST constants, so changing them is never treated as a mere comment.
    return ast.dump(before, include_attributes=False) == ast.dump(after, include_attributes=False)


def literal_class_change(old_tree, new_tree, touched):
    """Recognize a single class literal value change, with all else identical.

    Calls, imports, decorators, bases and assignments with multiple targets
    are intentionally not handled by this narrower rule.
    """
    def entries(tree):
        result = {}

        def visit(body, owners=()):
            for node in body:
                if isinstance(node, ast.ClassDef):
                    visit(node.body, owners + (node.name,))
                elif owners and isinstance(node, (ast.Assign, ast.AnnAssign)):
                    target = node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else (
                        node.target if isinstance(node, ast.AnnAssign) else None)
                    if isinstance(target, ast.Name) and node.value is not None:
                        key = owners + (target.id,)
                        result.setdefault(key, []).append(node)
        visit(tree.body)
        return result

    old_entries, new_entries = entries(old_tree), entries(new_tree)
    for key, candidates in old_entries.items():
        if len(candidates) != 1 or len(new_entries.get(key, [])) != 1:
            continue
        old, new = candidates[0], new_entries[key][0]
        if not touched or not all(old.lineno <= n <= old.end_lineno for n in touched):
            continue
        try:
            ast.literal_eval(old.value)
            ast.literal_eval(new.value)
        except (ValueError, TypeError, SyntaxError):
            continue
        # Mask just this value and require every other AST field to agree.
        old_value, new_value = old.value, new.value
        try:
            old.value = ast.Constant(value=None)
            new.value = ast.Constant(value=None)
            if same_ast(old_tree, new_tree):
                return key[-1]
        finally:
            old.value, new.value = old_value, new_value
    return None


def lambda_body_change(old_tree, new_tree, touched):
    old_lambdas = [n for n in ast.walk(old_tree) if isinstance(n, ast.Lambda)]
    new_lambdas = [n for n in ast.walk(new_tree) if isinstance(n, ast.Lambda)]
    if len(old_lambdas) != len(new_lambdas):
        return None
    for old, new in zip(old_lambdas, new_lambdas):
        if not touched or not all(old.body.lineno <= n <= old.body.end_lineno for n in touched):
            continue
        old_body, new_body = old.body, new.body
        try:
            old.body = ast.Constant(value=None)
            new.body = ast.Constant(value=None)
            if same_ast(old_tree, new_tree):
                # Single-line lambdas can lack a separate LINE event in
                # Python 3.13. Find the containing storage binding so its
                # recorded readers can cover this observability gap.
                parents = {child: parent for parent in ast.walk(old_tree)
                           for child in ast.iter_child_nodes(parent)}
                binding = None
                parent = parents.get(old)
                while parent is not None:
                    if isinstance(parent, ast.Assign):
                        if len(parent.targets) == 1:
                            target = parent.targets[0]
                            if isinstance(target, ast.Name):
                                binding = target.id
                            elif isinstance(target, ast.Attribute):
                                binding = target.attr
                        break
                    if isinstance(parent, ast.AnnAssign):
                        if isinstance(parent.target, ast.Name):
                            binding = parent.target.id
                        elif isinstance(parent.target, ast.Attribute):
                            binding = parent.target.attr
                        break
                    if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        break
                    parent = parents.get(parent)
                inline = old_body.lineno == old_body.end_lineno == old.lineno
                return old_body.lineno, old_body.end_lineno, binding, inline
        finally:
            old.body, new.body = old_body, new_body
    return None


def untraced_dispatch_tests(mapping, all_test_ids):
    """Baseline-derived uncertainty, not a table of mutation answers.

    The v1 recorder observes only its own process/thread. A parent test can
    submit arbitrary Python work whose internals are absent from this map.
    Find dispatch call sites in baseline source and use their recorded parent
    contexts as a conservative extra candidate set for Python edits.
    """
    result = set()
    contexts = mapping["contexts"]
    for entry in mapping["files"].values():
        source = entry.get("source", "")
        if not any(name in source for name in CONCURRENCY_MODULES):
            continue
        tree = ast.parse(source)
        imports = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports[alias.asname or alias.name.split(".")[0]] = alias.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    imports[alias.asname or alias.name] = node.module + "." + alias.name
        if not any(name.split(".")[0] in CONCURRENCY_MODULES for name in imports.values()):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute):
                is_dispatch = node.func.attr in DISPATCH_METHODS
            elif isinstance(node.func, ast.Name):
                full_name = imports.get(node.func.id, "")
                is_dispatch = (full_name.split(".")[0] in CONCURRENCY_MODULES
                               and full_name.rsplit(".", 1)[-1] in DISPATCH_METHODS)
            else:
                is_dispatch = False
            if is_dispatch:
                for lineno in range(node.lineno, node.end_lineno + 1):
                    result.update(contexts[n] for n in entry["lines"].get(str(lineno), []))
    return set(all_test_ids) if GLOBAL in result else result & all_test_ids


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    rows: list[str] = field(default_factory=list)


@dataclass
class Change:
    old_path: str | None = None
    path: str | None = None
    hunks: list[Hunk] = field(default_factory=list)
    unsupported: bool = False


def header_path(value):
    if value == "/dev/null":
        return None
    if value.startswith('"'):
        value = shlex.split(value)[0]
    else:
        value = value.split("\t")[0]
    if not value.startswith(("a/", "b/")):
        raise ValueError("unexpected patch path")
    value = value[2:]
    if value.startswith("/") or ".." in value.split("/"):
        raise ValueError("unsafe patch path")
    return value


def parse_patch(text):
    changes = []
    current = None
    hunk = None
    for line in text.splitlines():
        if line.startswith("diff --git "):
            current = Change()
            changes.append(current)
            hunk = None
        elif current is None:
            if line.strip():
                raise ValueError("expected a git unified diff")
        elif line.startswith("@@ "):
            m = HUNK.match(line)
            if not m:
                raise ValueError("invalid hunk header")
            a, b, c, d = m.groups()
            hunk = Hunk(int(a), int(b) if b is not None else 1,
                        int(c), int(d) if d is not None else 1)
            current.hunks.append(hunk)
        elif hunk is not None and line.startswith((" ", "+", "-")):
            hunk.rows.append(line)
        elif line.startswith("\\ No newline at end of file"):
            current.unsupported = True
        elif line.startswith("--- "):
            current.old_path = header_path(line[4:])
        elif line.startswith("+++ "):
            current.path = header_path(line[4:])
        elif line.startswith(("index ", "new file mode ", "deleted file mode ")):
            pass
        elif line.strip():
            current.unsupported = True
    if not changes:
        raise ValueError("empty or unsupported patch")
    for change in changes:
        if change.old_path != change.path or not change.path or not change.hunks:
            change.unsupported = True
        for block in change.hunks:
            if sum(row[0] != "+" for row in block.rows) != block.old_count:
                raise ValueError("old hunk length mismatch")
            if sum(row[0] != "-" for row in block.rows) != block.new_count:
                raise ValueError("new hunk length mismatch")
    return changes


def apply_to_source(source, change):
    """Validate old text exactly, reconstruct new source, record old anchors.

    Additions are anchored to both neighboring baseline lines so insertion
    between functions does not silently inherit just one function's coverage.
    """
    old = source.splitlines()
    result = []
    cursor = 0
    touched = set()
    for h in change.hunks:
        start = h.old_start - 1 if h.old_count else h.old_start
        if start < cursor or start > len(old):
            raise ValueError("overlapping/out-of-range hunk")
        result.extend(old[cursor:start])
        if len(result) != (h.new_start - 1 if h.new_count else h.new_start):
            raise ValueError("new hunk location mismatch")
        cursor = start
        removed = set()
        for row in h.rows:
            tag, content = row[0], row[1:]
            if tag in " -":
                if cursor >= len(old) or old[cursor] != content:
                    raise ValueError("patch does not match indexed baseline")
            if tag == "-":
                touched.add(cursor + 1)
                removed.add(cursor + 1)
            elif tag == "+":
                if removed:
                    touched.update(removed)
                else:
                    touched.update(n for n in (cursor, cursor + 1) if 1 <= n <= len(old))
            else:
                removed.clear()
            if tag != "+":
                cursor += 1
            if tag != "-":
                result.append(content)
    result.extend(old[cursor:])
    return "\n".join(result) + ("\n" if source.endswith("\n") else ""), touched


class Selector:
    def __init__(self, tests, mapping, *, features=None):
        self.tests = tests
        self.all_ids = {t["id"] for t in tests}
        self.mapping = mapping
        self.features = {"ast_equal", "lambda", "literal"} if features is None else set(features)
        self.ready = bool(mapping and mapping.get("schema") == 2 and mapping.get("pin") == PIN
                          and mapping.get("baseline_exit_code") == 0
                          and self.all_ids <= set(mapping.get("test_ids", []))
                          and self.all_ids <= set(mapping.get("observed_test_ids", [])))
        self.dispatch_tests = set()
        self.source_tests = set()
        self.reference_cache = {}
        if self.ready:
            try:
                self.dispatch_tests = untraced_dispatch_tests(mapping, self.all_ids)
                if "ast_equal" in self.features:
                    self.source_tests = source_observer_tests(mapping, self.all_ids)
            except (SyntaxError, ValueError, KeyError, TypeError, IndexError):
                # Failure to establish the unobserved-work boundary is not
                # evidence that it is empty.
                self.dispatch_tests = set(self.all_ids)
                self.source_tests = set(self.all_ids)

    def attribute_readers(self, name):
        if name not in self.reference_cache:
            readers = set()
            for entry in self.mapping["files"].values():
                source = entry.get("source", "")
                if name not in source:
                    continue
                for node in ast.walk(ast.parse(source)):
                    read = ((isinstance(node, ast.Attribute) and node.attr == name and isinstance(node.ctx, ast.Load))
                            or (isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load))
                            or (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                                and node.func.id in {"getattr", "hasattr"} and len(node.args) > 1
                                and isinstance(node.args[1], ast.Constant) and node.args[1].value == name))
                    if read:
                        for n in range(node.lineno, node.end_lineno + 1):
                            readers.update(entry["lines"].get(str(n), []))
            self.reference_cache[name] = readers
        return self.reference_cache[name]

    def choose(self, patch_text):
        if not self.ready:
            return self.tests, "fallback: missing/incompatible/incomplete coverage map"
        selected = set()
        used_dispatch_safety = False
        refinements = set()
        contexts = self.mapping["contexts"]

        def add(numbers, allow_global=False):
            names = {contexts[n] for n in numbers}
            if GLOBAL in names and not allow_global:
                raise ValueError("change touches globally executed/read baseline data")
            selected.update(names & self.all_ids)

        try:
            for change in parse_patch(patch_text):
                if change.unsupported:
                    raise ValueError("unsupported file operation")
                entry = self.mapping["files"].get(change.path)
                if entry is None:
                    raise ValueError("file absent from baseline index")
                if not change.path.endswith(".py"):
                    if not entry["reads"]:
                        raise ValueError("data file has no recorded readers")
                    add(entry["reads"])
                    continue
                new_source, touched = apply_to_source(entry["source"], change)
                old_tree = ast.parse(entry["source"], type_comments=True)
                new_tree = ast.parse(new_source, type_comments=True)
                if "ast_equal" in self.features and same_ast(old_tree, new_tree):
                    # A changed cookie/shebang can alter how the file is loaded,
                    # even though parsing the already-decoded text agrees.
                    if encoding_of(entry["source"]) != encoding_of(new_source):
                        raise ValueError("source encoding changed")
                    old_first = entry["source"].splitlines()[:1]
                    new_first = new_source.splitlines()[:1]
                    if old_first != new_first and any(s.startswith("#!") for s in old_first + new_first):
                        raise ValueError("executable shebang changed")
                    selected.update(self.source_tests)
                    refinements.add("ast-equal-with-source-observers")
                    continue
                selected.update(self.dispatch_tests)
                used_dispatch_safety = bool(self.dispatch_tests)
                literal = literal_class_change(old_tree, new_tree, touched) if "literal" in self.features else None
                if literal:
                    readers = self.attribute_readers(literal)
                    if not readers:
                        raise ValueError("class literal has no attributable readers")
                    add(readers)
                    refinements.add("class-literal-readers")
                    continue
                lambda_span = lambda_body_change(old_tree, new_tree, touched) if "lambda" in self.features else None
                if lambda_span:
                    a, b, binding, inline = lambda_span
                    body_contexts = set()
                    for n, covered in entry["lines"].items():
                        if a <= int(n) <= b:
                            body_contexts.update(covered)
                    body_names = {contexts[n] for n in body_contexts} & self.all_ids
                    if inline or not body_names:
                        if not binding:
                            raise ValueError("lambda has no reliable body coverage or storage binding")
                        readers = self.attribute_readers(binding)
                        if not readers:
                            raise ValueError("lambda binding has no attributable readers")
                        add(readers)
                        refinements.add("lambda-binding-readers")
                    add(body_contexts, allow_global=True)
                    refinements.add("lambda-body")
                    continue
                if not touched:
                    raise ValueError("no attributable baseline location")
                for lineno in touched:
                    # Only direct function-body changes use fine-grained coverage.
                    # A definition/decorator/signature is executed during import
                    # and is deliberately handled conservatively.
                    functions = [(a, b) for a, b in entry["functions"] if a < lineno <= b]
                    if not functions:
                        raise ValueError("module/class/definition-level edit")
                    direct = entry["lines"].get(str(lineno), [])
                    if direct:
                        add(direct)
                    else:
                        # Uncovered changed line / inserted line / comment:
                        # expand to the smallest enclosing baseline function.
                        a, b = min(functions, key=lambda x: x[1] - x[0])
                        for n, covered in entry["lines"].items():
                            if a < int(n) <= b:
                                add(covered)
            reason = "coverage+dispatch-safety" if used_dispatch_safety else "coverage"
            if refinements:
                reason += "+" + "+".join(sorted(refinements))
            return [t for t in self.tests if t["id"] in selected], reason
        except (ValueError, KeyError, IndexError, TypeError, SyntaxError) as error:
            return self.tests, f"fallback: {error}"
