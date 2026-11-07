"""Final package smoke checks after wheel installation."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import magpie_agent.cli
import magpie_agent.core

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--report", type=Path, default=Path.cwd() / "installed-smoke.json")
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
exe = shutil.which("magpie-agent")
cases = [
    (["version"], None, 0),
    (["agents", "get", "--input", "-", "--limit", "1"], b'\xef\xbb\xbf{"agent":"codex"}', 0),
    (["agents", "get", "--input", "-"], b'{"agent":"codex","agent":"serve"}', 2),
]
records = []
for argv, stdin, expected in cases:
    result = subprocess.run([exe, *argv], input=stdin or b"", capture_output=True, timeout=100)
    data = json.loads(result.stdout)
    records.append(dict(argv=argv, exit=result.returncode, response=data,
                        passed=result.returncode == expected and not result.stderr
                        and not result.stdout.startswith(b"\xef\xbb\xbf") and b"\x1b" not in result.stdout))
help_result = subprocess.run([exe, "--help"], capture_output=True, timeout=10)
help_ok = help_result.returncode == 0 and b"--timeout" in help_result.stdout
matches = {Path(m.__file__).name: Path(m.__file__).read_bytes() == (root / "src" / "magpie_agent" / Path(m.__file__).name).read_bytes()
           for m in (magpie_agent.cli, magpie_agent.core)}
wheel = root / "dist" / "magpie_agent_cli-0.2.0-py3-none-any.whl"
report = dict(cases=records, help_passed=help_ok, installed_matches_source=matches,
              wheel=str(wheel), wheel_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
              passed=all(c["passed"] for c in records) and help_ok and all(matches.values()))
args.report.resolve().write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"passed": report["passed"], "wheel_sha256": report["wheel_sha256"]}))
sys.exit(0 if report["passed"] else 1)
