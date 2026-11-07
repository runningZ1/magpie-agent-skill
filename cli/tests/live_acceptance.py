"""Read-only acceptance through the installed console entry point; no live commit."""
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=Path.cwd() / "live-acceptance.json")
    args = parser.parse_args()
    exe = shutil.which("magpie-agent")
    if not exe:
        raise RuntimeError("Installed console entry point not found")
    configs = [Path.home() / ".codex" / "config.toml", Path.home() / ".claude" / "settings.json"]
    def hashes():
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in configs if p.exists()}
    before = hashes()
    cases = [
        (["version"], 0), (["schema"], 0), (["errors"], 0), (["doctor"], 0),
        (["agents", "list", "--limit", "30"], 0),
        (["agents", "get", "--agent", "codex"], 0),
        (["models", "list", "--limit", "3", "--fields", "text"], 0),
        (["models", "list", "--limit", "3", "--offset", "3"], 0),
        (["presets", "list", "--limit", "5"], 0),
        (["providers", "list", "--limit", "10"], 0),
        (["profiles", "list"], 0), (["accounts", "status"], 0),
        (["usage", "get", "--period", "7d", "--limit", "12"], 0),
        (["agents", "set-model", "--agent", "codex", "--model", "codex/gpt-6.1-sol", "--dry-run"], 0),
        (["agents", "set-effort", "--agent", "codex", "--effort", "high", "--dry-run"], 0),
        (["tools", "list"], 3), (["logs", "get", "--lines", "20"], 3),
        (["models", "list", "--limit", "999"], 2),
        (["agents", "get", "--agent=--help"], 2),
        (["agents", "get", "--agent", "serve"], 3),
        (["version", "--output", "jsonl", "--quiet", "--no-input", "--no-color"], 0),
    ]
    def run(case):
        argv, expected = case
        start = time.monotonic()
        completed = subprocess.run([exe, *argv], capture_output=True, stdin=subprocess.DEVNULL, timeout=110)
        value = json.loads(completed.stdout.decode("utf-8"))
        valid = (completed.returncode == expected and value["ok"] == (expected == 0)
                 and not completed.stderr and not completed.stdout.startswith(b"\xef\xbb\xbf")
                 and b"\x1b" not in completed.stdout and completed.stdout.endswith(b"\n")
                 and len(completed.stdout.splitlines()) == 1)
        return dict(argv=argv, expected=expected, exit=completed.returncode, passed=valid,
                    seconds=round(time.monotonic() - start, 3), response=value,
                    stderr=completed.stderr.decode("utf-8", errors="replace"))
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(run, cases))
    after = hashes()
    report = dict(executable=exe, backend="real installed Magpie", cases=results,
                  config_sha256_before=before, config_sha256_after=after, configs_unchanged=before == after,
                  passed=all(r["passed"] for r in results) and before == after)
    target = args.report.resolve()
    target.write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "cases": len(results), "configs_unchanged": before == after,
                      "report": str(target), "failures": [r["argv"] for r in results if not r["passed"]]}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
