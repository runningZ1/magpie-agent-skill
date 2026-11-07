"""Run a bounded, read-only Magpie CLI probe using the Python standard library."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


CHECKS = {
    "version": ["--version"],
    "ls": ["ls"],
    "presets": ["presets"],
    "models": ["models"],
    "usage": ["usage", "7d"],
    "tools": ["tools"],
    "log": ["log"],
}


def decode(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", help="Path to the installed magpie executable")
    parser.add_argument("--checks", nargs="+", choices=CHECKS,
                        default=["version", "ls", "models", "presets"])
    parser.add_argument("--timeout", type=float, default=90,
                        help="Maximum seconds per command")
    parser.add_argument("--max-chars", type=int, default=12000,
                        help="Maximum characters per output stream")
    args = parser.parse_args()
    if args.timeout <= 0 or args.max_chars < 1:
        parser.error("--timeout and --max-chars must be positive")

    executable = args.exe or shutil.which("magpie")
    if not executable and os.name == "nt":
        candidate = Path.home() / ".local" / "bin" / "magpie.exe"
        if candidate.is_file():
            executable = str(candidate)
    if not executable:
        print(json.dumps({"ok": False, "error": "magpie executable not found"}))
        return 1

    results = []
    for name in args.checks:
        command = [executable, *CHECKS[name]]
        start = time.monotonic()
        item = {"check": name, "command": command}
        try:
            completed = subprocess.run(command, capture_output=True,
                                       timeout=args.timeout, shell=False)
            stdout, stderr = decode(completed.stdout), decode(completed.stderr)
            item.update(exit_code=completed.returncode,
                        ok=completed.returncode == 0, timed_out=False)
        except subprocess.TimeoutExpired as exc:
            stdout, stderr = decode(exc.stdout), decode(exc.stderr)
            item.update(exit_code=None, ok=False, timed_out=True)
        except OSError as exc:
            stdout, stderr = "", str(exc)
            item.update(exit_code=None, ok=False, timed_out=False)
        item.update(seconds=round(time.monotonic() - start, 3),
                    stdout=stdout[:args.max_chars], stderr=stderr[:args.max_chars],
                    stdout_truncated=len(stdout) > args.max_chars,
                    stderr_truncated=len(stderr) > args.max_chars)
        results.append(item)

    ok = all(item["ok"] for item in results)
    print(json.dumps({"ok": ok, "executable": executable, "results": results},
                     ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
