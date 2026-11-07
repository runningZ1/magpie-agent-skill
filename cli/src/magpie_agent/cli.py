"""One emitter for all terminal results, including argument failures."""
import argparse
import io
import json
import os
from pathlib import Path
import sys
import time
import threading
import uuid

from . import __version__
from .core import (Backend, ERRORS, Failure, OPS, Runner, clean, execute,
                   schema, text_result, validate)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Failure("INVALID_ARGUMENT", message, next_actions=[{"command": "magpie-agent --help"}])


def parser():
    root = Parser(description="Typed headless adapter for the official Magpie CLI. Default output: JSON. Example: magpie-agent models list --limit 20",
                  epilog="Common flags after an action: --output json|jsonl|text, --json, --timeout SECONDS, --limit N, --offset N, --fields line,text, --input FILE|-, --quiet, --no-input, --no-color. Writes default to preview; use --dry-run or --confirm HASH. Discover exact inputs with schema or action --help.")
    subs = root.add_subparsers(dest="resource", required=True)
    for name in ("version", "schema", "errors", "doctor"):
        common(subs.add_parser(name, help=f"Discover {name}"))
    for resource in sorted({name.split()[0] for name in OPS}):
        child = subs.add_parser(resource)
        actions = child.add_subparsers(dest="action", required=True)
        for name, spec in OPS.items():
            first, action = name.split()
            if first != resource:
                continue
            p = actions.add_parser(action, description=spec["description"] + (". Defaults to a backend-aware dry-run; commit with --confirm HASH." if spec.get("write") else ". Read-only."),
                                   epilog="Example: magpie-agent " + name + " --input - < request.json")
            common(p)
            for field, kind in spec.get("params", {}).items():
                p.add_argument("--" + field, type=int if kind == "lines" else str, help=f"Input type: {kind}; see schema")
            if spec.get("write"):
                group = p.add_mutually_exclusive_group()
                group.add_argument("--dry-run", action="store_true", help="Preview only (default)")
                group.add_argument("--confirm", help="Hash returned by the exact current preview")
    return root


def common(p):
    p.add_argument("--output", choices=("json", "jsonl", "text"), default="json")
    p.add_argument("--json", action="store_true", help="Alias for --output json")
    p.add_argument("--timeout", type=float, default=90, help="Total seconds; 0 < N <= 300")
    p.add_argument("--limit", type=int, default=100, help="Text line page size, 1..500")
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--fields", default="line,text", help="Comma-separated item fields: line,text")
    p.add_argument("--input", help="JSON input file, or - for stdin (maximum 64 KiB)")
    p.add_argument("--quiet", action="store_true", help="Only terminal output (no progress emitted)")
    p.add_argument("--no-input", action="store_true", help="Never prompt (always enforced)")
    p.add_argument("--no-color", action="store_true", help="Disable color (always enforced)")


def read_input(path, timeout=90):
    try:
        if path == "-":
            parts = []
            def read():
                try:
                    try:
                        fd = sys.stdin.fileno()
                    except (io.UnsupportedOperation, AttributeError):
                        fd = None
                    if fd is None:
                        parts.append(sys.stdin.buffer.read(65537))
                    else:
                        raw_chunks = bytearray()
                        while len(raw_chunks) < 65537:
                            block = os.read(fd, min(4096, 65537 - len(raw_chunks)))
                            if not block:
                                break
                            raw_chunks.extend(block)
                        parts.append(bytes(raw_chunks))
                except (OSError, ValueError):
                    parts.append(None)
            thread = threading.Thread(target=read, daemon=True)
            thread.start()
            thread.join(timeout)
            if thread.is_alive():
                raise Failure("TIMEOUT", "Timed out waiting for explicit JSON stdin")
            if not parts or parts[0] is None:
                raise ValueError()
            raw = parts[0]
        else:
            with Path(path).open("rb") as handle:
                raw = handle.read(65537)
        if len(raw) > 65536:
            raise Failure("INVALID_ARGUMENT", "JSON input exceeds 64 KiB")
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate input field")
                result[key] = value
            return result
        # Windows PowerShell may prefix redirected UTF-8 input with a BOM.
        values = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique)
        if not isinstance(values, dict):
            raise ValueError()
        return values
    except (OSError, ValueError, UnicodeError):
        raise Failure("INVALID_ARGUMENT", "Input must be an accessible UTF-8 JSON object") from None


def run(args):
    started = time.monotonic()
    if not 0 < args.timeout <= 300 or not 1 <= args.limit <= 500 or args.offset < 0:
        raise Failure("INVALID_ARGUMENT", "Require timeout (0,300], limit [1,500], offset >= 0")
    fields = args.fields.split(",")
    if not fields or set(fields) - {"line", "text"}:
        raise Failure("INVALID_ARGUMENT", "Valid --fields: line,text")
    op = args.resource + (" " + args.action if hasattr(args, "action") else "")
    if op in ("version", "schema", "errors", "doctor") and args.input:
        raise Failure("INVALID_ARGUMENT", "This command does not accept JSON business input")
    if op == "version":
        return dict(cli_version=__version__, schema_version="1.0", python=sys.version.split()[0]), {}
    if op == "schema":
        return schema(), {}
    if op == "errors":
        return schema()["errors"], {}
    if op == "doctor":
        backend = Backend(Runner(args.timeout))
        version = backend.call("--version").strip()
        help_text = backend.root_help()
        capabilities = {}
        for capability in ("ls", "models", "presets", "providers", "usage", "tools", "log"):
            try:
                backend.require(capability)
                capabilities[capability] = "advertised_not_executed"
            except Failure:
                capabilities[capability] = "not_advertised"
        return dict(backend=backend.identity, version=version, capabilities=capabilities,
                    health="discovery_only; does not prove gateway, authentication or inference", platform=sys.platform), {}
    spec = OPS[op]
    supplied = read_input(args.input, args.timeout) if args.input else {}
    for name in spec.get("params", {}):
        value = getattr(args, name)
        if value is not None:
            if name in supplied:
                raise Failure("INVALID_ARGUMENT", f"{name} supplied by both flag and JSON")
            if name == "enabled":
                if value not in ("true", "false"):
                    raise Failure("INVALID_ARGUMENT", "--enabled accepts true or false")
                value = value == "true"
            supplied[name] = value
    values = validate(op, supplied)
    remaining = args.timeout - (time.monotonic() - started)
    if remaining <= 0:
        raise Failure("TIMEOUT", "Total request budget exhausted")
    backend = Backend(Runner(remaining))
    if spec.get("write"):
        return execute(backend, op, values, confirm=args.confirm, request_id=args.request_id), {}
    if "capability" in spec:
        backend.require(spec["capability"])
    if op == "agents get":
        backend.agent(values["agent"])
        text = backend.call(values["agent"])
    elif op == "usage get":
        text = backend.call("usage", values["period"])
    elif op == "logs get":
        text = backend.call("log", "-n", str(values["lines"]))
    else:
        text = backend.call(*spec["argv"])
    data, meta = text_result(text, op, args.limit, args.offset, fields)
    if meta["has_more"]:
        argv = ["magpie-agent", *op.split(), "--offset", str(meta["next_offset"]), "--limit", str(args.limit), "--fields", args.fields]
        for key, value in values.items():
            argv += ["--" + key, str(value).lower() if type(value) is bool else str(value)]
        meta["next_action"] = {"argv": argv}
    meta["backend"] = backend.identity
    return data, meta


def emit(payload, output):
    # text is formatted JSON too; no raw backend text ever reaches the terminal.
    sys.stdout.write(json.dumps(payload, ensure_ascii=True, indent=2 if output == "text" else None) + "\n")
    sys.stdout.flush()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    started = time.monotonic()
    request_id = "req_" + uuid.uuid4().hex
    meta = {"request_id": request_id}
    output = "json"
    try:
        args = parser().parse_args()
        args.request_id = request_id
        output = "json" if args.json else args.output
        meta["command"] = args.resource + (" " + args.action if hasattr(args, "action") else "")
        data, extra = run(args)
        meta.update(extra)
        payload, status = dict(schema_version="1.0", ok=True, type="result", data=data, meta=meta), 0
    except Failure as exc:
        status, retryable = ERRORS[exc.code]
        payload = dict(schema_version="1.0", ok=False, type="error", error=dict(
            code=exc.code, message=clean(exc.message), retryable=retryable, outcome=exc.outcome,
            details=exc.details, next_actions=exc.next_actions), meta=meta)
    except KeyboardInterrupt:
        payload, status = dict(schema_version="1.0", ok=False, type="error", error=dict(
            code="INTERRUPTED", message="Cancelled", retryable=False, outcome="unknown", details={}, next_actions=[]), meta=meta), 130
    except Exception:
        payload, status = dict(schema_version="1.0", ok=False, type="error", error=dict(
            code="INTERNAL_ERROR", message="Unexpected local failure; use request_id for diagnostics", retryable=False,
            outcome="not_started", details={}, next_actions=[{"command": "magpie-agent doctor"}]), meta=meta), 1
    meta["duration_ms"] = round((time.monotonic() - started) * 1000)
    try:
        emit(payload, output)
    except BrokenPipeError:
        # A downstream consumer deliberately closed the pipe; no traceback.
        return 0
    return status


if __name__ == "__main__":
    sys.exit(main())
