"""Function-entry and explicit-source-read capture for the final attempt."""
import os
import sys
from collections import defaultdict

from trace_map import Recorder


class CompleteRecorder(Recorder):
    def __init__(self, root):
        super().__init__(root)
        self.entries = defaultdict(lambda: defaultdict(set))

    def function_start(self, code, offset):
        path = self.relative(code.co_filename)
        if path and path.startswith(("sqlglot/", "tests/")):
            self.entries[path][code.co_firstlineno].add(self.context)
        return sys.monitoring.DISABLE

    def start(self):
        if not hasattr(sys, "monitoring"):
            raise RuntimeError("Complete capture requires Python 3.12+, preferably the pinned 3.13 image")
        super().start()
        if self.tool_id is None:
            super().stop()
            raise RuntimeError("No monitoring tool ID available for complete capture")
        mon = sys.monitoring
        mon.register_callback(self.tool_id, mon.events.PY_START, self.function_start)
        mon.set_events(self.tool_id, mon.events.LINE | mon.events.PY_START)
        mon.restart_events()

    def stop(self):
        if self.tool_id is not None:
            sys.monitoring.register_callback(self.tool_id, sys.monitoring.events.PY_START, None)
        super().stop()

    def audit(self, event, args):
        super().audit(event, args)
        if not self.active or event != "open":
            return
        filename, mode, flags = args
        if (isinstance(mode, str) and any(c in mode for c in "wax")) or (
            isinstance(flags, int) and flags & os.O_ACCMODE == os.O_WRONLY
        ):
            return
        path = self.relative(filename)
        if not path or not path.endswith(".py") or not path.startswith(("sqlglot/", "tests/")):
            return
        # Import loaders read .py files too; these reads do not make every
        # comment observable. Keep explicit source reads from project code,
        # while excluding import machinery and pytest assertion rewriting.
        frame = sys._getframe(1)
        for _ in range(40):
            if frame is None:
                break
            name = frame.f_code.co_filename.replace("\\", "/")
            if "importlib" in name or "_pytest/assertion/rewrite" in name:
                return
            project = self.relative(name)
            if project and project.startswith(("sqlglot/", "tests/")):
                self.reads[path].add(self.context)
                return
            frame = frame.f_back

    def export(self, test_ids, exit_code=0, pin=None):
        from trace_map import PIN
        mapping = super().export(test_ids, exit_code, PIN if pin is None else pin)
        index = {name: n for n, name in enumerate(mapping["contexts"])}
        for path, entries in self.entries.items():
            if path in mapping["files"]:
                mapping["files"][path]["entries"] = {
                    str(line): sorted(index[name] for name in names)
                    for line, names in entries.items()
                }
        mapping["function_block_selection"] = True
        mapping["conservative_initialization"] = True
        mapping["explicit_source_reads"] = True
        observed = set(mapping["observed_test_ids"])
        for rows in self.entries.values():
            for names in rows.values():
                observed.update(names)
        mapping["observed_test_ids"] = sorted(observed & set(test_ids))
        return mapping
