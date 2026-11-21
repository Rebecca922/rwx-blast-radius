"""One final capture/evaluate/container-check/push pipeline, with rollback.

No unseen labels are available. Public sample labels are used only in the
development gate. Runtime still reads only pinned-baseline maps and inputs.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from impact import Selector
from score import evaluate_run
from selector import best_split
from trace_map import PIN, load_map

REPO = "https://github.com/Rebecca922/rwx-blast-radius"
FILES = ["Dockerfile", "selector.py", "impact.py", "trace_map.py", "map.json.gz",
         "trace_v2.py", "worker_trace.py", "collect_final.py", "final_attempt.py",
         "collect_map.py", "Dockerfile.finalmap", "requirements.lock", "Makefile", "score.py", "test_final.py", "test_v1.py",
         "test_v11.py", "test_v12.py", "FINAL_METHOD_ZH.md", ".gitignore", ".dockerignore"]


def run(args, **kwargs):
    print("+ " + " ".join(map(str, args)), flush=True)
    return subprocess.run(args, check=True, **kwargs)


def evaluate(root, mapping):
    tests = json.loads((root / "sample/tests.json").read_text())["tests"]
    answers = json.loads((root / "sample/answers.json").read_text())["commits"]
    selector = Selector(tests, mapping)
    if not selector.ready:
        raise RuntimeError("sample inventory is not completely covered by the baseline map")
    output = []
    fallbacks = 0
    for path in sorted((root / "sample/commits").glob("*.patch")):
        selected, reason = selector.choose(path.read_text())
        workers = best_split(selected)
        output.append(json.dumps({"commit": path.stem, "workers": workers}))
        fallbacks += reason.startswith("fallback:")
        print(f"{path.stem}: {len(selected)} tests, {len(workers)} workers; {reason}", flush=True)
    result = evaluate_run(("\n".join(output) + "\n").encode(), tests, answers)
    if not result["ok"]:
        raise RuntimeError(result)
    return {"simulated_seconds": result["simulated_milliseconds"] / 1000,
            "misses": len(result["missed_commits"]), "fallback_commits": fallbacks,
            "untraced_dispatch_tests": len(selector.dispatch_tests)}, tests, answers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--push", action="store_true", help="commit and push to the already authorized RWX repository after validation")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    os.chdir(root)
    if not shutil.which("docker"):
        parser.error("Docker Desktop must be installed and running")
    if not (root / "map.json.gz").is_file():
        parser.error("existing map.json.gz is required for the control and rollback")
    if args.push:
        remote = subprocess.check_output(["git", "remote", "get-url", "origin"], text=True).strip().rstrip("/")
        if remote not in {REPO, REPO + ".git", "git@github.com:Rebecca922/rwx-blast-radius.git"}:
            parser.error(f"unexpected GitHub remote: {remote}")
        staged = subprocess.check_output(["git", "diff", "--cached", "--name-only"], text=True).splitlines()
        if set(staged) - set(FILES):
            parser.error("unrelated files are staged; preserve them before running --push")
    for filename in (".gitignore", ".dockerignore"):
        path = root / filename
        text = path.read_text() if path.exists() else ""
        if ".rwx-final" not in text:
            path.write_text(text.rstrip() + "\n.rwx-final/\n")
    (root / ".rwx-final").mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="run-", dir=root / ".rwx-final"))
    backup = work / "previous-map.json.gz"
    shutil.copy2(root / "map.json.gz", backup)
    print("[1/6] Verify implementation", flush=True)
    run([sys.executable, "-B", "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py", "-q"])
    print("[2/6] Reuse source/dependency layers", flush=True)
    run(["docker", "info"], stdout=subprocess.DEVNULL)
    image = "blast-radius-final-mapbuilder"
    run(["docker", "build", "--platform", "linux/amd64", "-f", "Dockerfile.finalmap", "--target", "mapbuild", "-t", image, "."])
    print("[3/6] Capture unchanged baseline INCLUDING child tasks; this is the expensive step", flush=True)
    start = time.monotonic()
    run(["docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none", "--cpus", "4",
         "--env", "HOME=/tmp", "--volume", str(work) + ":/out", image,
         "python", "-B", "/app/collect_final.py", "--source", "/opt/sqlglot",
         "--output", "/out/new-map.json.gz", "--source-verified-by-build"])
    new_path = work / "new-map.json.gz"
    new = load_map(new_path)
    proof = new.get("process_capture", {})
    if not (new.get("pin") == PIN and new.get("baseline_exit_code") == 0
            and proof.get("complete") and proof.get("expected_tasks", 0) > 0
            and proof.get("expected_tasks") == proof.get("completed_tasks")):
        raise SystemExit("child-task completeness check failed; previous map preserved")
    if new_path.stat().st_size > 18_000_000:
        raise SystemExit("new map exceeds storage allowance; previous map preserved")
    print("[4/6] Compare against the existing map on exactly the same public samples", flush=True)
    old_report, _, _ = evaluate(root, load_map(backup))
    new_report, tests, answers = evaluate(root, new)
    report = {"scope": "public samples only; not an unseen-data guarantee", "previous": old_report,
              "candidate": new_report, "completed_child_tasks": proof["completed_tasks"],
              "capture_wall_seconds": round(time.monotonic() - start, 2)}
    (work / "comparison.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)
    if new_report["misses"] or new_report["simulated_seconds"] >= old_report["simulated_seconds"]:
        raise SystemExit(f"candidate did not pass the improvement gate; previous map kept. Diagnostics: {work}")
    promoted = False
    try:
        os.replace(new_path, root / "map.json.gz")
        promoted = True
        print("[5/6] Build and validate the final image under scorer limits", flush=True)
        run(["make", "sample-existing"])
        result = evaluate_run((root / "output.jsonl").read_bytes(), tests, answers)
        if not result["ok"] or result.get("missed_commits"):
            raise RuntimeError(f"container validation failed: {result}")
        if result["simulated_milliseconds"] != round(new_report["simulated_seconds"] * 1000):
            raise RuntimeError("host/container selection differs")
    except BaseException:
        if promoted:
            shutil.copy2(backup, root / "map.json.gz")
        raise
    print("[6/6] Validated candidate ready", flush=True)
    if args.push:
        run(["git", "add", "--", *FILES])
        tracked = subprocess.check_output(["git", "ls-files", "-z"]).split(b"\0")
        total = sum((root / os.fsdecode(p)).stat().st_size for p in tracked if p and (root / os.fsdecode(p)).is_file())
        if total > 20_000_000:
            raise SystemExit("tracked repository exceeds 20 MB; not pushed")
        run(["git", "commit", "-m", "Capture test-attributed child coverage and select complete function blocks"])
        run(["git", "push", "origin", "HEAD"])
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        print(f"\nRepository: {REPO}\nCommit: {head}\nSubmit this commit on the RWX page.", flush=True)
    else:
        print("Run with --push to commit and push after validation.", flush=True)


if __name__ == "__main__":
    main()
