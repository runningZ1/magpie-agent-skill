"""Typed operations, bounded official-process adapter and machine contract."""
from __future__ import annotations

import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager

from . import __version__

SOURCE = "https://usemagpie.ai/docs/zh/start#cli"
ERRORS = {
    "INVALID_ARGUMENT": (2, False), "DEPENDENCY_MISSING": (3, False),
    "UNSUPPORTED_CAPABILITY": (3, False), "AUTH_REQUIRED": (3, False),
    "TIMEOUT": (4, True), "OUTPUT_LIMIT": (4, False),
    "SERVICE_UNAVAILABLE": (4, True), "CONFLICT": (5, False),
    "BACKEND_FAILED": (1, False), "INVALID_BACKEND_OUTPUT": (1, False),
    "INTERNAL_ERROR": (1, False), "OUTCOME_UNKNOWN": (1, False),
    "INTERRUPTED": (130, False),
}

# The registry drives parsing, discovery, validation and effect classification.
OPS = {
    "agents list": dict(argv=["ls"], description="Detected Agent settings"),
    "agents get": dict(params={"agent": "identifier"}, description="One Agent's configuration"),
    "models list": dict(argv=["models"], description="Selectable provider/model IDs"),
    "providers list": dict(argv=["providers"], description="Providers with credential-bearing lines suppressed"),
    "presets list": dict(argv=["presets"], description="Built-in vendor presets"),
    "profiles list": dict(argv=["profiles"], capability="profiles", description="Existing settings profiles; prerequisite for save/apply"),
    "accounts status": dict(argv=["accounts", "discovery"], capability="accounts discovery", description="Current sign-in discovery setting; does not report authentication validity"),
    "usage get": dict(params={"period": "period"}, defaults={"period": "7d"}, description="Token/cost report with source labels"),
    "tools list": dict(argv=["tools"], capability="tools", description="Official CLI tool discovery"),
    "logs get": dict(params={"lines": "lines"}, defaults={"lines": 100}, capability="log", description="Bounded application log"),
    "agents set-model": dict(params={"agent": "identifier", "model": "model"}, write=True, description="Set an Agent model and read it back"),
    "agents set-effort": dict(params={"agent": "identifier", "effort": "effort"}, write=True, description="Set reasoning effort and read it back"),
    "agents reset": dict(params={"agent": "identifier"}, write=True, description="Remove Magpie integration from one Agent"),
    "profiles save": dict(params={"name": "name"}, write=True, description="Save current Agent settings as a profile"),
    "profiles apply": dict(params={"name": "name"}, write=True, description="Apply an existing profile"),
    "accounts discovery": dict(params={"enabled": "boolean"}, write=True, description="Enable or disable upstream sign-in discovery"),
}
EXCLUDED = {
    "provider add": "Official key-in-argv interface conflicts with secret handling requirements",
    "accounts add": "Interactive login; no verified headless two-phase upstream contract",
    "accounts switch": "Non-idempotent next-account action not exposed in this version",
    "tool generate_image": "Current backend lacks tools; no verified end-to-end invocation",
    "tui/tray/claude launcher": "Interactive/background application lifecycle remains with official CLI",
    "log -f": "Unbounded follow intentionally replaced by logs get",
}


class Failure(Exception):
    def __init__(self, code, message, *, details=None, outcome="not_started", next_actions=None):
        super().__init__(message)
        self.code, self.message = code, message
        self.details, self.outcome = details or {}, outcome
        self.next_actions = next_actions or []


def clean(text):
    """Defense in depth; sensitive provider lines additionally suppressed wholesale."""
    text = re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))", "", str(text))
    text = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", "[REDACTED PRIVATE KEY]", text, flags=re.S)
    text = re.sub(r"(?im)^.*(?:authorization|api[_ -]?key|password|client[_ -]?secret|access[_ -]?token|refresh[_ -]?token|cookie)\s*[:=].*$", "[REDACTED CREDENTIAL LINE]", text)
    text = re.sub(r"\b(?:sk-|sk_|oc_|ghp_|gho_|eyJ)[A-Za-z0-9._~+/-]{5,}", "[REDACTED]", text)
    text = re.sub(r"(?i)\bBearer\s+[^\s]+", "Bearer [REDACTED]", text)
    text = re.sub(r"(?i)(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text)
    text = re.sub(r"(?i)([?&](?:token|key|secret|signature|x-amz-signature|x-amz-credential)=)[^&\s]+", r"\1[REDACTED]", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[REDACTED EMAIL]", text)
    return "".join(c for c in text if c in "\n\t" or (ord(c) >= 32 and ord(c) != 127 and not 0x200B <= ord(c) <= 0x200F and not 0x202A <= ord(c) <= 0x202E and not 0x2066 <= ord(c) <= 0x2069))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def field_value(text, field):
    # Official output is e.g. "Codex effort medium", not just "medium".
    match = re.fullmatch(r"[^\r\n]+\s+" + re.escape(field) + r"\s+(\S+)", text.strip())
    if not match:
        raise Failure("INVALID_BACKEND_OUTPUT", "Cannot parse official Agent field output", details={"field": field})
    return match.group(1)


def discovery_value(text):
    match = re.match(r"account discovery is (on|off)(?::|$)", text.strip())
    if not match:
        raise Failure("INVALID_BACKEND_OUTPUT", "Cannot parse official discovery state")
    return match.group(1)


def validate(operation, values):
    spec = OPS[operation]
    expected = spec.get("params", {})
    if set(values) - set(expected):
        raise Failure("INVALID_ARGUMENT", "Unknown input fields", details={"allowed": list(expected)})
    result = {**spec.get("defaults", {}), **values}
    for key, kind in expected.items():
        if key not in result:
            raise Failure("INVALID_ARGUMENT", f"Missing --{key}", details={"field": key})
        value = result[key]
        valid = False
        if kind in ("identifier", "model", "name"):
            valid = isinstance(value, str) and 0 < len(value) <= 160 and not value.startswith("-") and clean(value) == value
            if kind == "identifier":
                valid = valid and bool(re.fullmatch(r"[a-z][a-z0-9@:_-]*", value))
            elif kind == "model":
                valid = valid and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/@:+\[\]-]*", value)) and "/" in value
            else:
                valid = valid and bool(re.fullmatch(r"[\w .-]+", value)) and value.strip() == value
        elif kind == "effort":
            valid = value in ("low", "medium", "high", "xhigh")
        elif kind == "period":
            valid = value in ("today", "7d", "30d", "all")
        elif kind == "lines":
            valid = type(value) is int and 1 <= value <= 200
        elif kind == "boolean":
            valid = type(value) is bool
        if not valid:
            raise Failure("INVALID_ARGUMENT", f"Invalid {key}; expected {kind}", details={"field": key, "type": kind})
    return result


def kill_tree(process):
    if os.name == "nt":
        subprocess.run([str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"),
                        "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=5)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class Runner:
    def __init__(self, timeout=90, max_bytes=2 * 1024 * 1024):
        self.deadline = time.monotonic() + timeout
        self.max_bytes = max_bytes

    def run(self, argv):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise Failure("TIMEOUT", "Total execution budget exhausted")
        env = {k: v for k, v in os.environ.items() if k.upper() in {
            "SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "USERPROFILE", "HOME", "APPDATA",
            "LOCALAPPDATA", "TEMP", "TMP", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
            "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
            "MAGPIE_NO_ACCOUNTS"}}
        env.update(NO_COLOR="1", TERM="dumb", MAGPIE_NO_STATS="1")
        try:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, env=env, cwd=str(Path.home()),
                                       shell=False, start_new_session=os.name != "nt",
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except OSError:
            raise Failure("DEPENDENCY_MISSING", "Cannot start the configured Magpie executable") from None
        chunks = [bytearray(), bytearray()]
        lock, overflow = threading.Lock(), threading.Event()
        total = 0

        def read(pipe, index):
            nonlocal total
            try:
                while block := pipe.read(4096):
                    with lock:
                        total += len(block)
                        if total > self.max_bytes:
                            overflow.set()
                            break
                        chunks[index].extend(block)
            finally:
                pipe.close()

        threads = [threading.Thread(target=read, args=(pipe, i), daemon=True)
                   for i, pipe in enumerate((process.stdout, process.stderr))]
        for thread in threads:
            thread.start()
        failure = None
        try:
            while process.poll() is None or any(t.is_alive() for t in threads):
                if overflow.is_set():
                    failure = Failure("OUTPUT_LIMIT", "Backend exceeded 2 MiB capture limit")
                    break
                if time.monotonic() >= self.deadline:
                    failure = Failure("TIMEOUT", "Backend exceeded total timeout")
                    break
                time.sleep(0.01)
        except KeyboardInterrupt:
            failure = Failure("INTERRUPTED", "Execution interrupted")
        finally:
            if failure or process.poll() is None:
                kill_tree(process)
            process.wait(timeout=5)
            for thread in threads:
                thread.join(timeout=1)
        if failure:
            raise failure
        if overflow.is_set():
            raise Failure("OUTPUT_LIMIT", "Backend exceeded capture limit")
        return process.returncode, *(bytes(chunk).decode("utf-8", errors="replace") for chunk in chunks)


class Backend:
    def __init__(self, runner, executable=None):
        configured = executable or os.environ.get("MAGPIE_AGENT_EXE")
        path = configured or shutil.which("magpie")
        if not path:
            path = Path.home() / ".local" / "bin" / ("magpie.exe" if os.name == "nt" else "magpie")
        self.path = Path(path).expanduser().resolve()
        if not self.path.is_file() or (os.name == "nt" and self.path.suffix.lower() != ".exe"):
            raise Failure("DEPENDENCY_MISSING", "Installed Magpie executable not found", next_actions=[{"command": "where.exe magpie"}])
        self.runner = runner
        with self.path.open("rb") as handle:
            self.identity = dict(path=str(self.path), sha256=hashlib.file_digest(handle, "sha256").hexdigest())
        self._help = None

    def call(self, *args):
        with self.path.open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != self.identity["sha256"]:
                raise Failure("CONFLICT", "Magpie executable changed during this request")
        rc, out, err = self.runner.run([str(self.path), *args])
        joined = (out + "\n" + err).lower()
        if rc:
            if "unknown agent" in joined or "unknown command" in joined:
                code = "UNSUPPORTED_CAPABILITY"
            elif any(x in joined for x in ("unauthorized", "not logged in", "authentication required", "forbidden")):
                code = "AUTH_REQUIRED"
            elif any(x in joined for x in ("connection refused", "rate limit", "temporarily unavailable")):
                code = "SERVICE_UNAVAILABLE"
            else:
                code = "BACKEND_FAILED"
            raise Failure(code, "Official Magpie command failed", details={"upstream_exit": rc, "diagnostic": clean(err or out)[:2000]},
                          next_actions=[{"command": "magpie-agent doctor"}])
        return out

    def root_help(self):
        if self._help is None:
            self._help = self.call("help")
        return self._help

    def require(self, command):
        if not re.search(r"(?m)^\s*magpie\s+" + re.escape(command) + r"(?:\s|$)", self.root_help()):
            raise Failure("UNSUPPORTED_CAPABILITY", f"Installed Magpie does not advertise {command}", details={"capability": command})

    def agent(self, agent):
        help_text = self.call(agent, "--help")
        # Root help also mentions serve/tray/accounts. Merely finding a command
        # name there must never authorize treating an administrative verb as an Agent.
        if not re.search(r"(?m)^\s*magpie\s+" + re.escape(agent) + r"\s+<model>\s+", help_text):
            raise Failure("UNSUPPORTED_CAPABILITY", "Agent-specific help not found")
        return help_text


def schema():
    commands = {}
    for name, spec in OPS.items():
        types = spec.get("params", {})
        props = {key: {"type": "integer" if kind == "lines" else "boolean" if kind == "boolean" else "string",
                       "description": "Validated " + kind} for key, kind in types.items()}
        for key, value in spec.get("defaults", {}).items():
            props[key]["default"] = value
        sample = {"agent": "codex", "model": "codex/gpt-6.1-sol", "effort": "high", "name": "work", "enabled": "false", "period": "7d", "lines": "20"}
        example = "magpie-agent " + name + "".join(" --" + key + " " + sample[key] for key in types)
        if spec.get("write"):
            example += " --dry-run"
        commands[name] = {
            "description": spec["description"], "parameters": spec.get("params", {}),
            "input_schema": {"type": "object", "additionalProperties": False, "properties": props,
                             "required": [key for key in types if key not in spec.get("defaults", {})]},
            "defaults": spec.get("defaults", {}), "effect": "local_write" if spec.get("write") else "read",
            "dry_run": bool(spec.get("write")), "auto_retry": False,
            "confirmation": "Current backend-aware plan_hash; max five-minute bucket" if spec.get("write") else None,
            "authentication": "Reused upstream local state; no credential material accepted",
            "is_idempotent": not spec.get("write") or name in ("agents set-model", "agents set-effort", "accounts discovery"),
            "example": example,
            "output": "plan_or_write_verification" if spec.get("write") else "text_lines",
        }
    return dict(cli_version=__version__, schema_version="1.0", source=SOURCE, source_verified="2026-11-07",
                commands=commands, local_commands=["version", "schema", "errors", "doctor"],
                errors={k: {"exit": v[0], "retryable": v[1]} for k, v in ERRORS.items()},
                parameter_types={"identifier": "lowercase Agent id; never leading dash", "model": "provider/model from current catalog",
                                 "effort": ["low", "medium", "high", "xhigh"], "period": ["today", "7d", "30d", "all"],
                                 "lines": {"min": 1, "max": 200}, "boolean": [True, False], "name": "1..160 Unicode word, space, dot, hyphen"},
                output_formats=["json", "jsonl", "text"], item_fields=["line", "text"],
                global_flags={"output": "json|jsonl|text", "json": "alias", "timeout": "total request seconds",
                              "limit": "returned text line count", "offset": "nonnegative line offset", "fields": "line,text projection",
                              "input": "UTF-8 JSON object file or -", "quiet": "terminal output only", "no-input": "no prompts",
                              "no-color": "no ANSI"},
                result_shape={"schema_version": "1.0", "ok": True, "type": "result", "data": {},
                              "meta": {"request_id": "req_...", "duration_ms": 0, "command": "resource action"}},
                limits=dict(default_limit=100, max_limit=500, max_output_bytes=2097152, max_input_bytes=65536,
                            default_timeout=90, max_timeout=300),
                pagination="Offset over freshly fetched text lines; no snapshot guarantee",
                stdin="--input - reads one JSON object; flags cannot duplicate its fields",
                execution="Official process, argv-only, explicit home cwd, filtered environment, host user permissions, no OS sandbox; no automatic retry",
                cancellation="Exit 130; terminate subprocess tree; write outcome may be unknown",
                excluded_capabilities=EXCLUDED)


def text_result(text, operation, limit, offset, fields):
    cleaned = clean(text)
    lines = cleaned.splitlines()
    if operation == "providers list":
        # Upstream explicitly includes keys. Preserve only provider labels, never credential columns.
        lines = [re.split(r"\s{2,}", line.strip(), maxsplit=1)[0] for line in lines if line.strip()]
    rows = [{"line": i + 1, "text": line} for i, line in enumerate(lines)]
    selected = rows[offset:offset + limit]
    items = [{k: row[k] for k in fields} for row in selected]
    data = dict(kind="text_lines", items=items, source="official_magpie_cli")
    if operation == "usage get":
        data["sources_present"] = [name for name, phrase in (("gateway", "through magpie"), ("local_sessions", "not through magpie")) if phrase in cleaned]
        data["billing_semantics"] = "Upstream estimates; unpriced calls possible; not an invoice"
    return data, dict(returned=len(items), limit=limit, offset=offset,
                      has_more=offset + limit < len(rows), next_offset=offset + limit if offset + limit < len(rows) else None,
                      fields=fields, complete_capture=True, redacted=True, captured_sha256=digest(cleaned))


@contextmanager
def write_lock(state_dir):
    state_dir.mkdir(parents=True, exist_ok=True)
    lock = state_dir / "write.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise Failure("CONFLICT", "Another write holds the local lock; reconcile before removing a stale lock", details={"lock": str(lock)}) from None
    try:
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        os.close(fd)
        lock.unlink(missing_ok=True)


def audit(state_dir, event):
    path = state_dir / "audit.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def plan(backend, operation, values):
    agent = values.get("agent")
    noop = False
    if operation.startswith("agents "):
        help_text = backend.agent(agent)
        before = backend.call(agent)
        current = before.strip()
        if operation == "agents set-model":
            catalog = backend.call("models")
            ids = {line.strip().split()[0] for line in catalog.splitlines() if line.strip()}
            if values["model"] not in ids:
                raise Failure("INVALID_ARGUMENT", "Model ID is not in the current Magpie catalog", next_actions=[{"command": "magpie-agent models list"}])
            # The explicit field query avoids substring matches against other fields.
            current_model = field_value(backend.call(agent, "model"), "model")
            noop = current_model == values["model"]
            argv = [agent, values["model"]]
        elif operation == "agents set-effort":
            if not re.search(r"\bfields:.*\beffort\b", help_text):
                raise Failure("UNSUPPORTED_CAPABILITY", "Agent has no advertised effort field")
            current = field_value(backend.call(agent, "effort"), "effort")
            noop = current == values["effort"]
            argv = [agent, "effort", values["effort"]]
        else:
            argv = [agent, "default"]
    elif operation.startswith("profiles "):
        backend.require("profiles")
        profiles = backend.call("profiles")
        before = backend.call("ls") + "\n" + profiles
        names = {re.split(r"\s{2,}", line.strip())[0] for line in profiles.splitlines() if line.strip()}
        if operation == "profiles apply" and values["name"] not in names:
            raise Failure("INVALID_ARGUMENT", "Profile not found in current profiles")
        argv = ["save" if operation == "profiles save" else "use", values["name"]]
    else:
        backend.require("accounts discovery")
        before = backend.call("accounts", "discovery")
        desired = "on" if values["enabled"] else "off"
        noop = discovery_value(before) == desired
        argv = ["accounts", "discovery", desired]
    payload = dict(operation=operation, inputs=values, backend=backend.identity, actor=getpass.getuser(),
                   before_sha256=digest(before), argv=argv, time_bucket=int(time.time() // 300))
    return dict(plan_hash=digest(payload), operation=operation, inputs=values, argv=argv,
                before=clean(before)[:12000], before_sha256=digest(before), backend=backend.identity,
                noop=noop, effect="local_write", expires_at=(payload["time_bucket"] + 1) * 300,
                retry_policy="never_auto_retry; reconcile unknown outcome", requires_new_agent_session=True)


def execute(backend, operation, values, *, confirm=None, state_dir=None, request_id=None):
    if confirm is None:
        return dict(state="planned", plan=plan(backend, operation, values))
    state_dir = state_dir or Path.home() / ".local" / "state" / "magpie-agent"
    with write_lock(state_dir):
        # Re-resolve after acquiring lock; no planner-provided argv is accepted.
        proposal = plan(backend, operation, values)
        if confirm != proposal["plan_hash"]:
            raise Failure("CONFLICT", "Plan expired, inputs/backend/state changed, or confirmation hash is invalid",
                          next_actions=[{"command": "magpie-agent " + operation + " --help"}])
        if proposal["noop"]:
            return dict(state="noop", plan=proposal, verified=True)
        event = dict(request_id=request_id, plan_hash=confirm, operation=operation, inputs=values, timestamp=time.time())
        audit(state_dir, {**event, "phase": "before_dispatch"})
        try:
            backend.call(*proposal["argv"])
            if operation.startswith("agents "):
                after = backend.call(values["agent"])
                field = "model" if operation.endswith("set-model") else "effort" if operation.endswith("set-effort") else None
                if field and field_value(backend.call(values["agent"], field), field) != values[field]:
                    raise Failure("OUTCOME_UNKNOWN", "Read-back field differs from requested value")
            elif operation == "profiles save":
                after = backend.call("profiles")
                if values["name"] not in {re.split(r"\s{2,}", line.strip())[0] for line in after.splitlines() if line.strip()}:
                    raise Failure("OUTCOME_UNKNOWN", "Saved profile not found on read-back")
            elif operation == "profiles apply":
                after = backend.call("ls")
            else:
                after = backend.call("accounts", "discovery")
                if discovery_value(after) != ("on" if values["enabled"] else "off"):
                    raise Failure("OUTCOME_UNKNOWN", "Discovery setting not confirmed")
            audit(state_dir, {**event, "phase": "readback_complete", "after_sha256": digest(after)})
        except (Exception, KeyboardInterrupt) as exc:
            try:
                audit(state_dir, {**event, "phase": "outcome_unknown"})
            except OSError:
                pass
            code = "INTERRUPTED" if isinstance(exc, KeyboardInterrupt) or getattr(exc, "code", None) == "INTERRUPTED" else "OUTCOME_UNKNOWN"
            raise Failure(code, "Dispatched write may have committed; inspect current state before any retry",
                          outcome="unknown", details={"cause": getattr(exc, "code", type(exc).__name__), "plan_hash": confirm},
                next_actions=[{"command": "magpie-agent agents list"}]) from None
    fully_verified = operation in ("agents set-model", "agents set-effort", "accounts discovery", "profiles save")
    return dict(state="changed" if fully_verified else "dispatched_read_back", plan_hash=confirm, after=clean(after)[:12000], verified=fully_verified,
                verification="field_readback" if operation in ("agents set-model", "agents set-effort", "accounts discovery") else "state_readback_only",
                requires_new_agent_session=True)
