"""Propagate test context into real ProcessPoolExecutor tasks.

Only unchanged baseline executions are observed. No mutation labels or
sample answers are consumed. Task arguments are serialized before dispatch
so their deserialization also happens inside the correct child context.
"""
import gzip
import itertools
import json
import os
import sys
import threading
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from multiprocessing.reduction import ForkingPickler
from multiprocessing.util import Finalize
from pathlib import Path

from trace_map import GLOBAL
from trace_v2 import CompleteRecorder

_worker = None
_worker_tokens = set()
_worker_pool = None
_worker_folder = None


def _flush_worker():
    if _worker is None:
        return
    _worker.stop()
    payload = {
        "pool": _worker_pool,
        "tokens": sorted(_worker_tokens),
        "lines": {p: {str(n): sorted(names) for n, names in rows.items()}
                  for p, rows in _worker.lines.items()},
        "entries": {p: {str(n): sorted(names) for n, names in rows.items()}
                    for p, rows in _worker.entries.items()},
        "reads": {p: sorted(names) for p, names in _worker.reads.items()},
    }
    path = Path(_worker_folder) / f"worker-{os.getpid()}-{_worker_pool}.json.gz"
    pending = path.with_suffix(".partial")
    with gzip.open(pending, "wt") as stream:
        json.dump(payload, stream, separators=(",", ":"))
    os.replace(pending, path)


def _initialize_worker(root, folder, pool_token, initializer, initargs):
    global _worker, _worker_folder, _worker_pool, _worker_tokens
    _worker = CompleteRecorder(Path(root))
    _worker_pool = pool_token
    _worker_folder = folder
    _worker_tokens = set()
    _worker.context = "@pool-init"
    _worker.start()
    # multiprocessing uses its own finalizers before os._exit, not just atexit.
    Finalize(None, _flush_worker, exitpriority=20)
    if initializer is not None:
        initializer(*initargs)


def _execute_task(context, token, payload):
    if _worker is None:
        raise RuntimeError("worker recorder was not initialized")
    _worker.context = context
    try:
        fn, args, kwargs = ForkingPickler.loads(payload)
        return fn(*args, **kwargs)
    finally:
        _worker_tokens.add(token)


class ProcessCapture:
    def __init__(self, recorder, folder):
        self.recorder = recorder
        self.folder = Path(folder).resolve()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.counter = itertools.count()
        self.pool_counter = itertools.count()
        self.expected = set()
        self.pool_contexts = defaultdict(set)
        self.handled_sites = defaultdict(lambda: defaultdict(set))
        self.lock = threading.Lock()
        self.pools = []

    def install(self):
        self.original_init = ProcessPoolExecutor.__init__
        self.original_submit = ProcessPoolExecutor.submit
        capture = self

        def initialize(pool, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs):
            pool_token = next(capture.pool_counter)
            pool._rwx_capture_pool = pool_token
            # Avoid host CPU-count inflation under Docker Desktop emulation.
            if max_workers is None:
                max_workers = min(4, (getattr(os, "process_cpu_count", os.cpu_count)() or 1))
            result = capture.original_init(
                pool, max_workers=max_workers, mp_context=mp_context,
                initializer=_initialize_worker,
                initargs=(str(capture.recorder.root), str(capture.folder), pool_token, initializer, initargs),
                **kwargs,
            )
            capture.pools.append(pool)
            return result

        def submit(pool, fn, /, *args, **kwargs):
            context = capture.recorder.context
            token = next(capture.counter)
            # Frame is the actual caller of submit, not the wrapper itself.
            frame = sys._getframe(1)
            path = capture.recorder.relative(frame.f_code.co_filename)
            line = frame.f_lineno
            payload = bytes(ForkingPickler.dumps((fn, args, kwargs)))
            future = capture.original_submit(pool, _execute_task, context, token, payload)
            with capture.lock:
                capture.expected.add(token)
                capture.pool_contexts[pool._rwx_capture_pool].add(context)
                if path:
                    capture.handled_sites[path][line].add(context)
            return future

        ProcessPoolExecutor.__init__ = initialize
        ProcessPoolExecutor.submit = submit

    def restore(self):
        ProcessPoolExecutor.__init__ = self.original_init
        ProcessPoolExecutor.submit = self.original_submit

    def finish(self):
        for pool in self.pools:
            pool.shutdown(wait=True)

    def merge(self):
        completed = set()
        worker_count = 0
        for path in sorted(self.folder.glob("worker-*.json.gz")):
            with gzip.open(path, "rt") as stream:
                data = json.load(stream)
            completed.update(data["tokens"])
            worker_count += 1
            pool_tests = self.pool_contexts[data["pool"]]

            def names(values):
                result = set(values)
                if "@pool-init" in result:
                    result.remove("@pool-init")
                    result.update(pool_tests)
                return result

            for field, destination in (("lines", self.recorder.lines), ("entries", self.recorder.entries)):
                for filename, rows in data[field].items():
                    for line, contexts in rows.items():
                        destination[filename][int(line)].update(names(contexts))
            for filename, contexts in data["reads"].items():
                self.recorder.reads[filename].update(names(contexts))
        missing = self.expected - completed
        if missing:
            raise RuntimeError(f"incomplete child coverage: {len(missing)} of {len(self.expected)} tasks unaccounted for")
        return {
            "version": 1, "complete": True, "expected_tasks": len(self.expected),
            "completed_tasks": len(completed), "worker_files": worker_count,
            "handled_sites": {p: {str(n): sorted(names) for n, names in rows.items()}
                              for p, rows in self.handled_sites.items()},
        }
