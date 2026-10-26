"""v3.7.1 hardening: the audit findings that were still open once the E2E policy moved to config.

Each check pins the failure the audit described, not just the new code path: a lock taken
from a live holder, a rollback that lost the error it was rolling back, an install that
died before its receipt, a job whose failed save kept its session locked, telemetry that
replaced the answer it was measuring, an OpenCode child that inherited every credential
in the parent shell, a browser read pointed at the metadata service, and the small
argv/redaction shapes beside them.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import http.server
from pathlib import Path

from tests.checks.support import assert_true
from tests.checks.support import no_console


# ------------------------------------------------------------------------------ locks


def _check_owned_lock() -> None:
    from exoma.utils.owned_lock import OwnedFileLock

    root = Path(tempfile.mkdtemp(prefix="aw-lock-"))
    path = root / "store.lock"
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **no_console())
    try:
        # A live foreign holder, however old its file, is waited for and never taken.
        path.write_text(json.dumps({"pid": sleeper.pid, "token": "foreign"}), encoding="utf-8")
        old = path.stat().st_mtime - 3600
        os.utime(path, (old, old))
        try:
            with OwnedFileLock(path, ttl_seconds=0.1, wait_seconds=0.3):
                taken = True
        except TimeoutError:
            taken = False
        assert_true(not taken, "a lock whose owner is still running must not be taken over")
        assert_true(
            json.loads(path.read_text(encoding="utf-8")).get("token") == "foreign",
            "and the live owner's lock file is left exactly as it was",
        )

        # A dead holder is replaced at once.
        done = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True, **no_console())
        path.write_text(json.dumps({"pid": int(done.stdout.strip()), "token": "dead"}), encoding="utf-8")
        with OwnedFileLock(path, ttl_seconds=30, wait_seconds=2) as lock:
            assert_true(
                json.loads(path.read_text(encoding="utf-8")).get("token") == lock.token,
                "a lock whose owner exited is taken without waiting out the TTL",
            )
        assert_true(not path.exists(), "and released cleanly by its new owner")

        # Release removes only a lock this holder still owns.
        lock = OwnedFileLock(path, ttl_seconds=30, wait_seconds=2)
        lock.__enter__()
        lock._close()  # let the "next writer" replace the file, as a steal would
        path.unlink()
        path.write_text(json.dumps({"pid": sleeper.pid, "token": "next-writer"}), encoding="utf-8")
        lock.__exit__(None, None, None)
        assert_true(
            path.exists() and json.loads(path.read_text(encoding="utf-8")).get("token") == "next-writer",
            "an earlier holder's exit deleted the lock a later writer now holds",
        )
    finally:
        sleeper.kill()
        sleeper.wait()
        shutil.rmtree(root, ignore_errors=True)


_COUNTER_WORKER = r"""
import json, sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from exoma.utils.owned_lock import OwnedFileLock
lock_path, counter = Path(sys.argv[2]), Path(sys.argv[3])
for _ in range(int(sys.argv[4])):
    with OwnedFileLock(lock_path, ttl_seconds=30, wait_seconds=60):
        value = int(counter.read_text() or 0)
        time.sleep(0.002)
        counter.write_text(str(value + 1))
"""


def _check_owned_lock_under_process_contention() -> None:
    """Several processes start against one dead owner's lock and then contend.

    Every one of them sees the dead pid at the start, which is the reclaim race: a slow
    reclaimer deleting the lock a fast one had just taken shows up here as a lost
    increment.
    """
    root = Path(tempfile.mkdtemp(prefix="aw-lock-race-"))
    try:
        lock_path, counter = root / "c.lock", root / "counter"
        counter.write_text("0")
        done = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True, **no_console())
        lock_path.write_text(json.dumps({"pid": int(done.stdout.strip()), "token": "dead"}), encoding="utf-8")
        script = root / "worker.py"
        script.write_text(_COUNTER_WORKER, encoding="utf-8")
        repo = str(Path(__file__).resolve().parents[2])
        workers, rounds = 6, 15
        procs = [
            subprocess.Popen([sys.executable, str(script), repo, str(lock_path), str(counter), str(rounds)], **no_console())
            for _ in range(workers)
        ]
        codes = [proc.wait(timeout=120) for proc in procs]
        assert_true(codes == [0] * workers, f"a contending writer failed: exit codes {codes}")
        total = int(counter.read_text())
        assert_true(
            total == workers * rounds,
            f"lost updates under contention from a dead owner's lock: {total} != {workers * rounds}",
        )
        assert_true(not lock_path.exists(), "the lock is released after the last writer")
    finally:
        shutil.rmtree(root, ignore_errors=True)


_GUARD_HOLDER = r"""
import sys, time
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from exoma.utils.owned_lock import _try_os_lock
handle = _try_os_lock(Path(sys.argv[2]))
print("held" if handle else "busy", flush=True)
time.sleep(float(sys.argv[3]))
"""


def _check_reclaim_guard_cannot_be_taken_from_a_live_holder() -> None:
    """A reclaimer paused inside the guard keeps it, however old the guard file looks.

    The age rule this replaced handed a paused reclaimer's guard to the next one after 5s,
    and the paused one then deleted the lock the next writer had taken meanwhile.
    """
    from exoma.utils.owned_lock import OwnedFileLock

    root = Path(tempfile.mkdtemp(prefix="aw-guard-"))
    try:
        lock_path = root / "g.lock"
        guard_path = root / "g.lock.reclaim"
        done = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True, **no_console())
        lock_path.write_text(json.dumps({"pid": int(done.stdout.strip()), "token": "dead"}), encoding="utf-8")
        script = root / "holder.py"
        script.write_text(_GUARD_HOLDER, encoding="utf-8")
        repo = str(Path(__file__).resolve().parents[2])
        holder = subprocess.Popen(
            [sys.executable, str(script), repo, str(guard_path), "30"], stdout=subprocess.PIPE, text=True
        , **no_console())
        try:
            assert_true(holder.stdout.readline().strip() == "held", "fixture: the holder took the guard")
            old = guard_path.stat().st_mtime - 3600
            os.utime(guard_path, (old, old))
            try:
                with OwnedFileLock(lock_path, ttl_seconds=0.1, wait_seconds=0.5):
                    taken = True
            except TimeoutError:
                taken = False
            assert_true(
                not taken and json.loads(lock_path.read_text(encoding="utf-8")).get("token") == "dead",
                "a reclaim ran while another live reclaimer held the guard",
            )
        finally:
            holder.kill()
            holder.wait()
        with OwnedFileLock(lock_path, ttl_seconds=30, wait_seconds=5):
            pass
        assert_true(not lock_path.exists(), "once the holder is gone its guard is free and the dead lock is reclaimed")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _check_store_locks_share_the_owner_rules() -> None:
    from exoma.core.evidence.e2e.knowledge import _Lock as E2EKnowledgeLock
    from exoma.core.evidence.fact_store import _FactLock
    from exoma.core.knowledge.store import _KnowledgeLock
    from exoma.utils.owned_lock import OwnedFileLock

    for lock in (_FactLock, _KnowledgeLock, E2EKnowledgeLock):
        assert_true(
            issubclass(lock, OwnedFileLock),
            f"{lock.__qualname__} carries its own copy of the lock again; the steal-from-a-live-holder bug lived in those copies",
        )


# ------------------------------------------------------------------------ durability


def _check_migration_rollback_failure_is_reported() -> None:
    import exoma.core.runtime.migrations as migrations
    from exoma.core.runtime.upgrade import upgrade_workflow_workspace
    from tests.checks.workspace_migration import _legacy_workspace

    root = _legacy_workspace()
    wf = root / ".workflow"
    saved_replace = migrations.os.replace
    saved_move = migrations.shutil.move

    def failing_replace(src, dst):
        if str(src).endswith("data.migrating"):
            raise OSError("simulated rename failure")
        return saved_replace(src, dst)

    def failing_move_back(src, dst):
        if "data.migrating" in str(src) and Path(dst).name == "usage.jsonl":
            raise OSError("simulated move-back failure")
        return saved_move(src, dst)

    migrations.os.replace = failing_replace
    migrations.shutil.move = failing_move_back
    try:
        try:
            upgrade_workflow_workspace(root, None)
            message = ""
        except ValueError as exc:
            message = str(exc)
        assert_true(
            "INCOMPLETE" in message and "usage.jsonl" in message,
            f"a rollback that could not put a file back was reported as clean: {message!r}",
        )
        assert_true(
            "simulated rename failure" in message,
            "the original failure must survive the rollback failure, not be replaced by it",
        )
        assert_true(
            (wf / "data.migrating" / "usage.jsonl").exists(),
            "the file that could not move back must stay in staging, not be deleted with it",
        )
        assert_true((wf / "sessions" / "s1").is_dir(), "entries that could move back did")
    finally:
        migrations.os.replace = saved_replace
        migrations.shutil.move = saved_move
        shutil.rmtree(root, ignore_errors=True)


def _check_installer_receipt_is_incremental() -> None:
    from exoma.installer import base

    root = Path(tempfile.mkdtemp(prefix="aw-receipt-"))
    try:
        base._reset_receipt()
        receipt = root / "backup" / "install_receipt.json"
        base._begin_receipt(receipt, {"version": "test"})
        assert_true(
            json.loads(receipt.read_text(encoding="utf-8")).get("complete") is False,
            "the receipt exists before the first destination is touched",
        )
        dest = root / "installed.txt"
        dest.write_text("one", encoding="utf-8")
        base._record("create", dest, "k/one", None, None)
        written = json.loads(receipt.read_text(encoding="utf-8"))
        assert_true(
            written.get("complete") is False and len(written.get("entries", [])) == 1,
            f"an install interrupted after its first write must already have a receipt: {written}",
        )
        base._flush_receipt(complete=True)
        written = json.loads(receipt.read_text(encoding="utf-8"))
        assert_true(written.get("complete") is True, "the final write marks the receipt complete")
        assert_true(
            not any(p.name.endswith(".tmp") for p in receipt.parent.iterdir()),
            "no temp file is left beside the receipt",
        )
        base._reset_receipt()
        empty = root / "empty" / "install_receipt.json"
        base._begin_receipt(empty, {"version": "test"})
        base._discard_empty_receipt()
        assert_true(not empty.exists(), "an apply that changed nothing leaves no receipt behind")
    finally:
        base._reset_receipt()
        shutil.rmtree(root, ignore_errors=True)


def _check_job_lock_released_when_save_fails() -> None:
    from exoma.core.jobs.job_manager import JobManager

    root = Path(tempfile.mkdtemp(prefix="aw-joblock-"))
    try:
        for finish in ("complete", "fail"):
            manager = JobManager(root / f"jobs-{finish}")
            job = manager.create_job("analyze", "t", f"sess-{finish}", str(root), None)
            lock = manager._lock_path(f"sess-{finish}")
            assert_true(lock.exists(), "fixture: creating a job takes the session lock")

            def broken_save(_job):
                raise OSError("simulated disk failure")

            manager._save = broken_save
            try:
                if finish == "complete":
                    manager.complete_job(job["job_id"], {"ok": True})
                else:
                    manager.fail_job(job["job_id"], "boom")
                raised = False
            except OSError:
                raised = True
            assert_true(raised, "the save failure itself is still raised")
            assert_true(
                not lock.exists(),
                f"{finish}_job left the session locked after its save failed",
            )
        # The record itself unreadable: the lock is still found by its job id.
        manager = JobManager(root / "jobs-corrupt")
        job = manager.create_job("analyze", "t", "sess-corrupt", str(root), None)
        manager._path(job["job_id"]).write_text("{not json", encoding="utf-8")
        try:
            manager.fail_job(job["job_id"], "boom")
        except Exception:  # noqa: BLE001 — the load failure itself may surface
            pass
        assert_true(
            not manager._lock_path("sess-corrupt").exists(),
            "a job whose record could not be read left its session locked",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _check_telemetry_failure_keeps_the_result() -> None:
    import exoma.core.provider.executor as executor_module
    from exoma.core.provider.executor import Executor
    from exoma.core.runtime.state import ensure_workflow_workspace
    from tests.checks.continuation import _EVIDENCE, _ScriptedAdapter, _session

    root = Path(tempfile.mkdtemp(prefix="aw-telemetry-"))
    saved = executor_module.write_call_meta

    def broken_write(*_args, **_kwargs):
        raise RuntimeError("simulated telemetry failure")

    executor_module.write_call_meta = broken_write
    try:
        ensure_workflow_workspace(root, os.getenv("AGENT_PATH"))
        result = Executor(adapter=_ScriptedAdapter([_EVIDENCE])).execute(
            "analyze", "map the thing", _session(), str(root)
        )
        assert_true(
            result.get("ok"),
            f"a telemetry failure in `finally` replaced the provider's answer: {result.get('content')!r}",
        )
        assert_true(
            "simulated telemetry failure" in str((result.get("meta") or {}).get("call_meta_error")),
            "the lost telemetry must be named on the result, not dropped silently",
        )
    finally:
        executor_module.write_call_meta = saved
        shutil.rmtree(root, ignore_errors=True)


# -------------------------------------------------------------------------- boundary


def _check_opencode_env_is_project_scoped() -> None:
    from exoma.adapters.providers.opencode_adapter import OpenCodeAdapter
    from exoma.adapters.shared.child_env import parse_dotenv, project_scoped_env

    root = Path(tempfile.mkdtemp(prefix="aw-env-"))
    try:
        (root / ".git").mkdir()
        (root / ".env").write_text(
            "# project keys\nexport ANTHROPIC_API_KEY=\"proj-key\"\nDB_URL='x'\nBAD LINE\nPORT=8000 # dev\n",
            encoding="utf-8",
        )
        parent = {
            "PATH": "/bin",
            "HOME": "/home/u",
            "XDG_DATA_HOME": "/home/u/.local/share",
            "OPENCODE_CONFIG": "/cfg.json",
            "AWS_SECRET_ACCESS_KEY": "parent-secret",
            "GITHUB_TOKEN": "parent-token",
        }
        env, meta = project_scoped_env(root, parent)
        assert_true(
            "AWS_SECRET_ACCESS_KEY" not in env and "GITHUB_TOKEN" not in env,
            "a credential from the parent shell reached the OpenCode child",
        )
        assert_true(
            env.get("HOME") == "/home/u" and env.get("XDG_DATA_HOME") and env.get("OPENCODE_CONFIG"),
            "HOME/XDG/OPENCODE_* must pass: that is how opencode finds auth.json and its config",
        )
        assert_true(
            env.get("ANTHROPIC_API_KEY") == "proj-key" and env.get("PORT") == "8000",
            f"the active project's .env keys must reach the child: {sorted(env)}",
        )
        assert_true(
            meta.get("project_dotenv") == "loaded" and meta.get("parent_env_dropped") == 2
            and "proj-key" not in json.dumps(meta),
            f"meta counts what was dropped and loaded, never a value: {meta}",
        )
        assert_true("BAD LINE" not in json.dumps(parse_dotenv("BAD LINE\n")), "an unparseable line is skipped")
        assert_true(
            parse_dotenv('TOKEN="abc" # note\nOPEN="unterminated\n') == {"TOKEN": "abc"},
            "a quoted value ends at its quote, and an unterminated one is skipped, not guessed",
        )

        # Through the adapter: a work_dir below the project root still finds its .env.
        sub = root / "src"
        sub.mkdir()
        adapter = OpenCodeAdapter(command="opencode")
        os.environ["AW_HARDENING_PARENT_SECRET"] = "must-not-pass"
        try:
            child = adapter._child_env(str(sub))
        finally:
            os.environ.pop("AW_HARDENING_PARENT_SECRET", None)
        assert_true(
            "AW_HARDENING_PARENT_SECRET" not in child and child.get("ANTHROPIC_API_KEY") == "proj-key",
            "the adapter must build the child env from the project root, not from os.environ",
        )
        assert_true(child.get("PYTHONUTF8") == "1", "the UTF-8 switches the adapter always set still apply")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _check_browser_read_refuses_metadata_addresses() -> None:
    import exoma.core.evidence.e2e.preflight as preflight

    saved = preflight.host_addresses
    answers = {
        "meta.example": ["169.254.169.254"],
        "aws6.example": ["fd00:ec2::254"],
        "mapped.example": ["::ffff:169.254.169.254"],
        "alibaba-mapped.example": ["::ffff:100.100.100.200"],
        "nat64.example": ["64:ff9b::a9fe:a9fe"],
        "alibaba-nat64.example": ["64:ff9b::6464:64c8"],
        "compat.example": ["::a9fe:a9fe"],
        "sixtofour.example": ["2002:a9fe:a9fe::"],
        # RFC 8215 local-use NAT64: /96 layout, then /48 layout (bytes 6-7 and 9-10).
        "nat64-local.example": ["64:ff9b:1::a9fe:a9fe"],
        "nat64-local48.example": ["64:ff9b:1:6464:64:c800::"],
        "staging.internal": ["10.1.2.3"],
        "public-nat64.example": ["64:ff9b:1::808:808"],
    }
    preflight.host_addresses = lambda host: answers[host]
    try:
        for host in (
            "meta.example",
            "aws6.example",
            "mapped.example",
            "alibaba-mapped.example",
            "nat64.example",
            "alibaba-nat64.example",
            "compat.example",
            "sixtofour.example",
            "nat64-local.example",
            "nat64-local48.example",
        ):
            origin = f"https://{host}"
            result = preflight.preflight(
                {"base_url": origin, "allow_remote": True, "allowed_origins": [origin]}, fake=True
            )
            assert_true(
                not result["ok"] and any(c["name"] == "read_policy" and not c["ok"] for c in result["checks"]),
                f"an allow-listed host resolving to a metadata address was accepted: {result}",
            )
        internal = "https://staging.internal"
        result = preflight.preflight(
            {"base_url": internal, "allow_remote": True, "allowed_origins": [internal]}, fake=True
        )
        assert_true(result["ok"], f"a private staging server is the ordinary remote target and must pass: {result}")
        public_nat64 = "https://public-nat64.example"
        result = preflight.preflight(
            {"base_url": public_nat64, "allow_remote": True, "allowed_origins": [public_nat64]}, fake=True
        )
        assert_true(result["ok"], f"a public host reached through local-use NAT64 must pass: {result}")
    finally:
        preflight.host_addresses = saved

    # The reachability probe reports a redirect instead of following it.
    hits: list[str] = []

    class Target(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            pass

    target = http.server.HTTPServer(("127.0.0.1", 0), Target)

    class Redirector(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/latest/meta-data/")
            self.end_headers()

        def log_message(self, *_args):
            pass

    redirector = http.server.HTTPServer(("127.0.0.1", 0), Redirector)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (target, redirector)]
    for t in threads:
        t.start()
    try:
        ok, detail = preflight.base_url_reachable(f"http://127.0.0.1:{redirector.server_port}/")
        assert_true(ok and "302" in detail, f"a redirect still proves a server answered: {detail}")
        assert_true(not hits, "the probe followed the redirect to another host")
    finally:
        for server in (target, redirector):
            server.shutdown()
            server.server_close()


# ------------------------------------------------------------------- small shapes


def _check_argv_and_redaction_shapes() -> None:
    from exoma.adapters.providers import opencode_adapter
    from exoma.adapters.providers.agy_adapter import AgyAdapter
    from exoma.core.evidence import fact_store, quick_verify
    from exoma.core.runtime.config_defaults import default_policies
    from exoma.utils.redact import redact

    assert_true(
        fact_store.RECURRENCE_THRESHOLD == default_policies()["fact_recurrence_threshold"],
        "the recurrence fallback drifted from the documented default again",
    )

    argv = quick_verify._SYNTAX_CHECKS[".js"][1]("--eval=process.exit(0).js")
    assert_true(
        argv[-1].startswith("./"),
        f"a git-reported file name reached a checker's argv where it reads as an option: {argv}",
    )

    @contextlib.contextmanager
    def cmd_parsing(applies: bool):
        original = opencode_adapter._cmd_parsing_applies
        opencode_adapter._cmd_parsing_applies = lambda: applies
        try:
            yield
        finally:
            opencode_adapter._cmd_parsing_applies = original

    class NoSpawn(AgyAdapter):
        spawned = False

        def _popen_capture(self, *args, **kwargs):
            NoSpawn.spawned = True
            raise AssertionError("spawned")

    with cmd_parsing(True):
        refused = NoSpawn(command="C:/tools/agy.cmd", timeout_seconds=5).run(
            'x" & echo INJECTED & "', {"session_id": "s", "provider_session_id": None}
        )
    assert_true(
        (refused.get("meta") or {}).get("error_type") == "unsafe_command_line" and not NoSpawn.spawned,
        f"agy launched through a .cmd shim with cmd.exe metacharacters in its argv: {refused}",
    )

    for text, leaks in (
        ('password: "hunter2x"', "hunter2x"),
        ("password: s3cretpw", "s3cretpw"),
        ("see https://user:pa55w0rd@example.com/x", "pa55w0rd"),
        ("amqp://guest:guest@rabbit:5672", "guest@"),
    ):
        clean, hits = redact(text)
        assert_true(leaks not in clean and hits, f"redaction let a credential through: {text!r} -> {clean!r}")
    for prose in ("password: required", "git@github.com:org/repo.git", "https://example.com/@user"):
        clean, hits = redact(prose)
        assert_true(clean == prose and not hits, f"redaction ate ordinary text: {prose!r} -> {clean!r}")


def _test_hardening() -> None:
    _check_owned_lock()
    _check_owned_lock_under_process_contention()
    _check_reclaim_guard_cannot_be_taken_from_a_live_holder()
    _check_store_locks_share_the_owner_rules()
    _check_migration_rollback_failure_is_reported()
    _check_installer_receipt_is_incremental()
    _check_job_lock_released_when_save_fails()
    _check_telemetry_failure_keeps_the_result()
    _check_opencode_env_is_project_scoped()
    _check_browser_read_refuses_metadata_addresses()
    _check_argv_and_redaction_shapes()
