"""Record baseline execution and file reads; no sample labels are used.

Uses Python's monitoring API where available, with settrace for older Python.
Pytest adapter lives in collect_map.py; this engine has no third-party deps.
"""
from __future__ import annotations

import ast
import gzip
import hashlib
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
GLOBAL = "@global"


class Recorder:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.prefix = str(self.root) + os.sep
        self._context = GLOBAL
        self.active = False
        self.lines = defaultdict(lambda: defaultdict(set))
        self.reads = defaultdict(set)
        self.code_paths = {}
        self.path_cache = {}
        self.tool_id = None
        self.old_trace = None

    @property
    def context(self):
        return self._context

    @context.setter
    def context(self, value):
        if value != self._context:
            self._context = value
            if self.tool_id is not None:
                # A line only needs one event in a test, but must become
                # observable again when the next test starts.
                sys.monitoring.restart_events()

    def relative(self, filename):
        if not isinstance(filename, (str, bytes, os.PathLike)):
            return None
        filename = os.fsdecode(filename)
        if filename.startswith("<"):
            return None
        if not os.path.isabs(filename):
            filename = os.path.abspath(filename)
        if filename not in self.path_cache:
            # root.resolve() and code.co_filename must use the same spelling.
            # On macOS /var/... may name the same file as /private/var/....
            canonical = os.path.realpath(filename)
            self.path_cache[filename] = (
                canonical[len(self.prefix):].replace(os.sep, "/")
                if canonical.startswith(self.prefix) else None
            )
        return self.path_cache[filename]

    def line(self, code, lineno):
        if code not in self.code_paths:
            path = self.relative(code.co_filename)
            self.code_paths[code] = path if path and path.startswith(("sqlglot/", "tests/")) else None
        path = self.code_paths[code]
        if path:
            self.lines[path][lineno].add(self.context)
        if self.tool_id is not None:
            # Disable this LINE location in the interpreter, rather than
            # paying for a Python callback on every loop iteration. The
            # context setter re-arms locations between tests.
            return sys.monitoring.DISABLE

    def trace(self, frame, event, arg):
        if event == "line":
            self.line(frame.f_code, frame.f_lineno)
        # Do not install a local trace in unrelated libraries.
        if event == "call":
            code = frame.f_code
            if code not in self.code_paths:
                path = self.relative(code.co_filename)
                self.code_paths[code] = path if path and path.startswith(("sqlglot/", "tests/")) else None
            if not self.code_paths[code]:
                return None
        return self.trace

    def audit(self, event, args):
        if not self.active or event != "open":
            return
        filename, mode, flags = args
        if (isinstance(mode, str) and any(c in mode for c in "wax")) or (
            isinstance(flags, int) and flags & os.O_ACCMODE == os.O_WRONLY
        ):
            return
        path = self.relative(filename)
        if path and path.startswith(("sqlglot/", "tests/")) and not path.endswith((".py", ".pyc")):
            self.reads[path].add(self.context)

    def start(self):
        self.active = True
        sys.addaudithook(self.audit)
        if hasattr(sys, "monitoring"):
            mon = sys.monitoring
            for tool_id in range(3, 6):
                try:
                    mon.use_tool_id(tool_id, "rwx-baseline-map")
                    self.tool_id = tool_id
                    break
                except ValueError:
                    continue
            if self.tool_id is not None:
                mon.register_callback(self.tool_id, mon.events.LINE, self.line)
                mon.set_events(self.tool_id, mon.events.LINE)
                mon.restart_events()
                return
        self.old_trace = sys.gettrace()
        sys.settrace(self.trace)

    def stop(self):
        self.active = False
        if self.tool_id is not None:
            mon = sys.monitoring
            mon.set_events(self.tool_id, 0)
            mon.register_callback(self.tool_id, mon.events.LINE, None)
            mon.free_tool_id(self.tool_id)
            self.tool_id = None
        else:
            sys.settrace(self.old_trace)

    def export(self, test_ids, exit_code=0, pin=PIN):
        """Store numeric context indexes instead of repeating long test names."""
        contexts = sorted({GLOBAL, *test_ids})
        index = {name: n for n, name in enumerate(contexts)}
        files = {}
        # Include unexecuted Python files too, so zero coverage is explicit.
        paths = {p.relative_to(self.root).as_posix() for folder in ("sqlglot", "tests")
                 for p in (self.root / folder).rglob("*.py")}
        paths.update(self.lines)
        paths.update(self.reads)
        for path in sorted(paths):
            disk = self.root / path
            if not disk.is_file():
                continue
            raw = disk.read_bytes()
            entry = {
                "sha256": hashlib.sha256(raw).hexdigest(),
                "lines": {str(n): sorted(index[x] for x in names)
                          for n, names in sorted(self.lines[path].items())},
                "reads": sorted(index[x] for x in self.reads[path]),
            }
            if path.endswith(".py"):
                # Source is needed to locate additions and verify the diff.
                source = raw.decode("utf-8-sig")
                entry["source"] = source
                tree = ast.parse(source)
                entry["functions"] = sorted(
                    [[node.lineno, node.end_lineno] for node in ast.walk(tree)
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
                )
            files[path] = entry
        observed = set()
        for line_map in self.lines.values():
            for names in line_map.values():
                observed.update(names)
        observed.discard(GLOBAL)
        return {"schema": 2, "pin": pin, "baseline_exit_code": exit_code,
                "observed_test_ids": sorted(observed),
                "contexts": contexts, "test_ids": sorted(test_ids), "files": files}


def save_map(path: Path, mapping):
    with gzip.GzipFile(filename=str(path), mode="wb", mtime=0) as stream:
        stream.write(json.dumps(mapping, separators=(",", ":")).encode())


def load_map(path: Path):
    with gzip.open(path, "rt") as stream:
        return json.load(stream)
