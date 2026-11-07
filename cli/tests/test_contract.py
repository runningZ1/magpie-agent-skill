import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from magpie_agent import cli
from magpie_agent.core import (Backend, ERRORS, Failure, OPS, Runner, clean, digest,
                               execute, field_value, plan, schema, text_result, validate, write_lock)


class FakeBackend:
    def __init__(self):
        self.identity = {"path": "fixture", "sha256": "fixture_sha"}
        self.model, self.effort = "demo/old", "medium"
        self.enabled = "on"
        self.profiles = {"work"}
        self.writes = []
        self.fail_write = False
        self.fail_readback = False

    def agent(self, agent):
        return "  magpie codex <model>\nfields: model, effort"

    def require(self, command):
        pass

    def call(self, *args):
        if self.fail_readback and self.writes and args == ("codex",):
            raise Failure("SERVICE_UNAVAILABLE", "readback unavailable")
        if args == ("models",):
            return "  demo/old  Old model\n  demo/new  新模型\n"
        if args == ("ls",) or args == ("codex",):
            return f"Codex {self.model} effort {self.effort}"
        if args == ("codex", "model"):
            return f"Codex model {self.model}\n"
        if args == ("codex", "effort"):
            return f"Codex effort {self.effort}\n"
        if args == ("profiles",):
            return "\n".join(f"  {p}  codex {self.model}" for p in sorted(self.profiles))
        if args == ("accounts", "discovery"):
            return f"account discovery is {self.enabled}: fixture"
        self.writes.append(args)
        if self.fail_write:
            raise Failure("TIMEOUT", "ambiguous dispatch")
        if len(args) == 2 and args[0] == "codex":
            self.model = args[1]
        elif args[:2] == ("codex", "effort"):
            self.effort = args[2]
        elif args[0] == "save":
            self.profiles.add(args[1])
        elif args[:2] == ("accounts", "discovery"):
            self.enabled = args[2]
        return "ok"


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.backend = FakeBackend()
        self.values = {"agent": "codex", "model": "demo/new"}

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, argv, backend=None):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["magpie-agent", *argv]), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch.object(cli, "Backend", return_value=backend or self.backend):
            status = cli.main()
        return status, json.loads(out.getvalue()), err.getvalue(), out.getvalue()

    def test_discovery_and_jsonl(self):
        for op in ("version", "schema", "errors"):
            for fmt in ("json", "jsonl", "text"):
                status, value, err, raw = self.invoke([op, "--output", fmt, "--quiet"])
                self.assertEqual(status, 0)
                self.assertTrue(value["ok"])
                self.assertNotIn("error", value)
                self.assertFalse(err)
                self.assertTrue(raw.endswith("\n"))
                self.assertNotIn("\x1b", raw)
                if fmt != "text":
                    self.assertEqual(len(raw.splitlines()), 1)

    def test_registry_schema(self):
        self.assertEqual(set(schema()["commands"]), set(OPS))
        self.assertEqual(set(schema()["errors"]), set(ERRORS))

    def test_invalid_commands_are_json(self):
        for argv in (["nonsense"], ["models", "li"], ["agents", "set-model"], ["models", "list", "--limit", "501"], ["models", "list", "--fields", "secret"]):
            status, value, _, _ = self.invoke(argv)
            self.assertEqual(status, 2)
            self.assertEqual(value["error"]["code"], "INVALID_ARGUMENT")
            self.assertNotIn("data", value)

    def test_stdin_composition(self):
        wrapped = io.TextIOWrapper(io.BytesIO(b'{"agent":"codex"}'), encoding="utf-8")
        with patch.object(sys, "stdin", wrapped):
            status, value, _, _ = self.invoke(["agents", "get", "--input", "-", "--fields", "text"])
        self.assertEqual(status, 0)
        self.assertEqual(set(value["data"]["items"][0]), {"text"})

    def test_powershell_bom_input(self):
        wrapped = io.TextIOWrapper(io.BytesIO(b'\xef\xbb\xbf{"agent":"codex"}'), encoding="utf-8")
        with patch.object(sys, "stdin", wrapped):
            status, value, _, _ = self.invoke(["agents", "get", "--input", "-"])
        self.assertEqual(status, 0)
        self.assertTrue(value["ok"])

    def test_json_sources_conflict(self):
        p = self.state / "request.json"
        p.write_text('{"agent":"codex"}', encoding="utf-8")
        status, value, _, _ = self.invoke(["agents", "get", "--input", str(p), "--agent", "codex"])
        self.assertEqual(status, 2)

    def test_input_errors(self):
        for body in (b"[]", b"invalid", b"x" * 65537, b'{"agent":"codex","unknown":1}', b'{"agent":"codex","agent":"serve"}'):
            p = self.state / "bad.json"
            p.write_bytes(body)
            status, _, _, _ = self.invoke(["agents", "get", "--input", str(p)])
            self.assertEqual(status, 2)

    def test_argument_and_option_injection(self):
        for value in ("--help", "codex;calc", "$(calc)", "codex\x1b[31m", "../codex", "codex\u202eexe", "codex\u200b"):
            with self.assertRaises(Failure):
                validate("agents get", {"agent": value})
        with self.assertRaises(Failure):
            validate("agents set-model", {"agent": "codex", "model": "--config=x"})
        status, value, _, _ = self.invoke(["models", "list", "--", "--help"])
        self.assertEqual(status, 2)

    def test_pagination_projection(self):
        text = "one\ntwo\nthree\nfour"
        data, meta = text_result(text, "models list", 2, 0, ["text"])
        self.assertEqual(data["items"], [{"text": "one"}, {"text": "two"}])
        self.assertEqual(meta["next_offset"], 2)
        rest, rest_meta = text_result(text, "models list", 2, 2, ["line", "text"])
        self.assertEqual(rest["items"][0]["line"], 3)
        self.assertFalse(rest_meta["has_more"])

    def test_secret_canaries(self):
        tokens = ["sk-CanarySecret123456", "oc_CanarySecret123456", "canaryPassword", "sessionCookie123"]
        text = f"Authorization: Bearer {tokens[0]}\napi_key={tokens[1]}\npassword={tokens[2]}\ncookie={tokens[3]}\n\x1b[31mred\u202etext\nhttps://a:b@site/x?token=canaryToken123"
        result = clean(text)
        for token in tokens + ["canaryToken123"]:
            self.assertNotIn(token, result)
        self.assertNotIn("\x1b", result)
        self.assertNotIn("\u202e", result)
        data, _ = text_result("  Vendor  id  host  bareCanarySecret  3 models", "providers list", 100, 0, ["text"])
        self.assertNotIn("bareCanarySecret", str(data))

    def test_usage_source_semantics(self):
        data, _ = text_result("no calls through magpie\nnot through magpie · session files", "usage get", 100, 0, ["text"])
        self.assertIn("local_sessions", data["sources_present"])
        self.assertIn("not an invoice", data["billing_semantics"])

    def test_dry_run_never_writes(self):
        with patch("magpie_agent.core.time.time", return_value=1800):
            result = execute(self.backend, "agents set-model", self.values)
        self.assertEqual(result["state"], "planned")
        self.assertEqual(self.backend.writes, [])

    def commit(self, operation="agents set-model", values=None):
        values = values or self.values
        proposal = plan(self.backend, operation, values)
        return execute(self.backend, operation, values, confirm=proposal["plan_hash"], state_dir=self.state, request_id="req_test")

    def test_model_commit_noop_and_audit(self):
        result = self.commit()
        self.assertEqual(result["state"], "changed")
        self.assertTrue(result["verified"])
        self.assertEqual(self.backend.model, "demo/new")
        result = self.commit()
        self.assertEqual(result["state"], "noop")
        self.assertEqual(len(self.backend.writes), 1)
        events = [json.loads(x) for x in (self.state / "audit.jsonl").read_text().splitlines()]
        self.assertEqual([x["phase"] for x in events], ["before_dispatch", "readback_complete"])
        self.assertFalse((self.state / "write.lock").exists())

    def test_effort_commit(self):
        self.assertTrue(self.commit("agents set-effort", {"agent": "codex", "effort": "high"})["verified"])
        self.assertEqual(self.backend.effort, "high")

    def test_discovery_commit_and_noop(self):
        values = {"enabled": False}
        self.assertTrue(self.commit("accounts discovery", values)["verified"])
        self.assertEqual(self.commit("accounts discovery", values)["state"], "noop")

    def test_profile_save(self):
        result = self.commit("profiles save", {"name": "测试方案"})
        self.assertTrue(result["verified"])
        self.assertIn("测试方案", self.backend.profiles)

    def test_profiles_apply_marks_limited_evidence(self):
        result = self.commit("profiles apply", {"name": "work"})
        self.assertFalse(result["verified"])
        self.assertEqual(result["state"], "dispatched_read_back")

    def test_reset_marks_limited_evidence(self):
        result = self.commit("agents reset", {"agent": "codex"})
        self.assertFalse(result["verified"])

    def test_profile_not_found(self):
        with self.assertRaises(Failure) as ctx:
            self.commit("profiles apply", {"name": "missing"})
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        self.assertEqual(self.backend.writes, [])

    def test_hash_tamper_state_and_expiry(self):
        for change in ("hash", "state", "expiry", "backend"):
            with patch("magpie_agent.core.time.time", return_value=1800):
                proposal = plan(self.backend, "agents set-model", self.values)
            confirm = proposal["plan_hash"]
            if change == "hash":
                confirm = "0" * 64
            if change == "state":
                self.backend.effort = "high"
            if change == "backend":
                self.backend.identity["sha256"] = "changed"
            with patch("magpie_agent.core.time.time", return_value=2400 if change == "expiry" else 1800):
                with self.assertRaises(Failure) as ctx:
                    execute(self.backend, "agents set-model", self.values, confirm=confirm, state_dir=self.state)
            self.assertEqual(ctx.exception.code, "CONFLICT")
        self.assertEqual(self.backend.writes, [])

    def test_unknown_outcome_no_retry(self):
        for failure in ("fail_write", "fail_readback"):
            self.backend = FakeBackend()
            setattr(self.backend, failure, True)
            with self.assertRaises(Failure) as ctx:
                self.commit()
            self.assertEqual(ctx.exception.outcome, "unknown")
            self.assertEqual(ctx.exception.code, "OUTCOME_UNKNOWN")
            self.assertEqual(len(self.backend.writes), 1)

    def test_cancelled_write_retains_unknown_outcome(self):
        original = self.backend.call
        def call(*args):
            if args == ("codex", "demo/new"):
                raise Failure("INTERRUPTED", "cancelled during dispatch")
            return original(*args)
        self.backend.call = call
        with self.assertRaises(Failure) as ctx:
            self.commit()
        self.assertEqual(ctx.exception.code, "INTERRUPTED")
        self.assertEqual(ctx.exception.outcome, "unknown")
        self.assertEqual(ERRORS[ctx.exception.code][0], 130)

    def test_every_public_command_help(self):
        for command in ["", "version", "schema", "errors", "doctor", *OPS]:
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream), self.assertRaises(SystemExit) as ctx:
                cli.parser().parse_args([*command.split(), "--help"])
            self.assertEqual(ctx.exception.code, 0)
            self.assertIn("usage:", stream.getvalue())

    def test_audit_failure_prevents_dispatch(self):
        with patch("magpie_agent.core.audit", side_effect=OSError("full")):
            with self.assertRaises(OSError):
                self.commit()
        self.assertEqual(self.backend.writes, [])

    def test_lock_conflict(self):
        with write_lock(self.state):
            with self.assertRaises(Failure) as ctx:
                self.commit()
        self.assertEqual(ctx.exception.code, "CONFLICT")

    def test_missing_backend(self):
        with self.assertRaises(Failure) as ctx:
            Backend(Runner(), executable=str(self.state / "not-here.exe"))
        self.assertEqual(ERRORS[ctx.exception.code][0], 3)

    def test_backend_error_matrix(self):
        backend = Backend.__new__(Backend)
        backend.path = Path(sys.executable)
        with backend.path.open("rb") as handle:
            import hashlib
            backend.identity = {"sha256": hashlib.file_digest(handle, "sha256").hexdigest()}
        for message, expected in (("unknown agent tools", "UNSUPPORTED_CAPABILITY"), ("not logged in", "AUTH_REQUIRED"), ("connection refused", "SERVICE_UNAVAILABLE"), ("mysterious failure", "BACKEND_FAILED")):
            backend.runner = unittest.mock.Mock()
            backend.runner.run.return_value = (1, "", message)
            with self.assertRaises(Failure) as ctx:
                backend.call("test")
            self.assertEqual(ctx.exception.code, expected)

    def test_administrative_command_cannot_be_agent(self):
        backend = Backend.__new__(Backend)
        backend.call = unittest.mock.Mock(return_value="magpie — help\n  magpie serve [--web] run gateway\n  magpie <agent> <model> set model\n")
        with self.assertRaises(Failure):
            backend.agent("serve")
        backend.call.assert_called_once_with("serve", "--help")

    def test_field_parser_fail_closed(self):
        self.assertEqual(field_value("Codex model demo/new\n", "model"), "demo/new")
        with self.assertRaises(Failure):
            field_value("Root help is not field output", "model")

    def test_subprocess_timeout(self):
        with self.assertRaises(Failure) as ctx:
            Runner(0.15).run([sys.executable, "-c", "import time; time.sleep(10)"])
        self.assertEqual(ctx.exception.code, "TIMEOUT")

    def test_subprocess_output_flood(self):
        with self.assertRaises(Failure) as ctx:
            Runner(5, max_bytes=8192).run([sys.executable, "-c", "import sys; sys.stdout.write('x'*100000)"])
        self.assertEqual(ctx.exception.code, "OUTPUT_LIMIT")

    def test_subprocess_literal_argv_closed_stdin(self):
        canary = "$(calc); & echo secret | extra"
        rc, out, err = Runner(5).run([sys.executable, "-c", "import json,sys; print(json.dumps([sys.argv[1],sys.stdin.read()]))", canary])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), [canary, ""])
        self.assertFalse(err)

    def test_internal_error_redacted(self):
        self.backend.call = unittest.mock.Mock(side_effect=ValueError("sk-secretCanary123456"))
        status, value, _, raw = self.invoke(["models", "list"])
        self.assertEqual(status, 1)
        self.assertEqual(value["error"]["code"], "INTERNAL_ERROR")
        self.assertNotIn("secretCanary", raw)


if __name__ == "__main__":
    unittest.main()
