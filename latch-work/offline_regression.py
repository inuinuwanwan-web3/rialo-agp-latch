"""Run unchanged AGP tests with synthetic configuration and offline guards.

Usage: python -B offline_regression.py targets|all
Environment changes are confined to this disposable test process.
"""
import ast
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

import pytest

WORK = Path(__file__).resolve().parent
AGP = WORK.parent / "agp-track-race-agent"
TARGETS = [
    "tests/test_config.py::ConfigTests::test_expands_codex_and_parent_environment_without_a_shell",
    "tests/test_codex_solver.py::test_codex_solver_uses_chatgpt_auth_isolation_and_schema",
    "tests/test_solver_flow.py::test_solver_flow_without_real_mcp",
]
COUNTS = {"agp": Counter(), "latch": Counter()}
BLOCKED = Counter()
GROUPS = {}
ISOLATION_TESTS = [
    "tests/test_manual_start.py::test_two_processes_at_most_once",
    "tests/test_start_reconcile.py::test_two_real_processes_share_at_most_once_store",
    "tests/test_track_watcher.py::test_two_processes_no_corruption_or_duplicate_events",
]


def source_hashes():
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
            for folder in ("agp_race_agent", "tests", "scripts")
            for p in (AGP / folder).rglob("*.py")}


def literal_script(filename):
    tree = ast.parse((AGP / "tests" / filename).read_text())
    return next(ast.literal_eval(node.args[0]) for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "write_text" and node.args)


def install_guards():
    scripts = {
        "fake_solver.py": literal_script("test_race_ready_flow.py"),
        "stderr_child.py": literal_script("test_mcp_recovery.py"),
    }
    tree = ast.parse((AGP / "tests/test_write_safety.py").read_text())
    child_code = next(ast.literal_eval(n.value) for n in ast.walk(tree)
                      if isinstance(n, ast.Assign) and any(
                          isinstance(t, ast.Name) and t.id == "code" for t in n.targets))

    def reject(kind):
        BLOCKED[kind] += 1
        raise AssertionError("Offline guard blocked " + kind)

    def audit(event, args):
        if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo",
                     "socket.gethostbyname", "socket.sendto", "socket.sendmsg"}:
            reject("network")
        if event == "os.system":
            reject("shell")
        if event == "subprocess.Popen":
            _, argv, _, _ = args
            if not isinstance(argv, (list, tuple)) or argv[0] != sys.executable:
                reject("subprocess")
            if len(argv) == 2:
                script = Path(argv[1]).resolve()
                if (not script.is_relative_to("/tmp") or script.name not in scripts
                        or script.read_text() != scripts[script.name]):
                    reject("subprocess")
            elif not (len(argv) == 5 and list(argv[1:3]) == ["-B", "-c"]
                      and argv[3] == child_code and Path(argv[4]).resolve().is_relative_to("/tmp")):
                reject("subprocess")
        if event in {"open", "sqlite3.connect"}:
            value = args[0]
            if not isinstance(value, (str, bytes, os.PathLike)):
                return
            value = os.fsdecode(value)
            if value == ":memory:":
                return
            if value.startswith("file:"):
                from urllib.parse import unquote, urlsplit
                value = unquote(urlsplit(value).path)
            path = Path(value).resolve()
            if path.is_relative_to(Path.home()) and not (
                path.is_relative_to(WORK) or any(path.is_relative_to(AGP / d)
                for d in ("agp_race_agent", "tests", "scripts", ".venv"))
                or path == AGP / "pyproject.toml"
            ):
                reject("private_file")
            if event == "sqlite3.connect" and not path.is_relative_to("/tmp"):
                reject("database")
            if event == "open":
                mode, flags = args[1:3]
                writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                    isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
                if writing and not (path.is_relative_to("/tmp") or path.is_relative_to(WORK)
                                    or str(path) == "/dev/null"):
                    reject("write")

    sys.addaudithook(audit)


@pytest.fixture(autouse=True)
def synthetic_solver_config(request, tmp_path, monkeypatch):
    if request.node.name == "test_solver_flow_without_real_mcp":
        (tmp_path / "config.toml").write_text(
            '[mcp]\nsource="direct"\ncommand="offline-unused"\n'
            '[solver]\ncommand="offline-unused-solver"\n'
            '[budget]\nmax_usd=1.0\n'
        )
        monkeypatch.chdir(tmp_path)


def pytest_collection_modifyitems(items):
    for item in items:
        GROUPS[item.nodeid] = "latch" if item.path.name.startswith("test_latch_") else "agp"


def pytest_runtest_logreport(report):
    group = GROUPS[report.nodeid]
    if report.when == "call" or report.failed or report.skipped:
        COUNTS[group][report.outcome] += 1


def main():
    mode = sys.argv[1] if len(sys.argv) == 2 else ""
    if mode not in {"targets", "all", "isolation", "adapter", "proxy"}:
        raise SystemExit("Expected targets, all, isolation, adapter or proxy")
    before = source_hashes()
    # Do not pass inherited credentials to any test or allowed local child.
    # HOME is retained unchanged, never redirected to a different account.
    for key in list(os.environ):
        if key not in {"HOME", "PATH", "LANG", "LC_ALL", "TZ"}:
            del os.environ[key]
    sys.path.insert(0, str(AGP))
    install_guards()
    temporary_root = Path(tempfile.mkdtemp(prefix="latch-offline-regression-", dir="/tmp"))
    # Isolate multiprocessing's backing files too; keep /dev/shm writes blocked.
    import multiprocessing.heap
    multiprocessing.heap.Arena._dir_candidates = [str(temporary_root)]
    selected = (TARGETS if mode == "targets" else ISOLATION_TESTS if mode == "isolation"
                else [str(WORK / "test_latch_proxy_contract.py")] if mode == "proxy"
                else [str(WORK / "test_latch_adapter_contract.py")] if mode == "adapter"
                else ["tests", *map(str, sorted(WORK.glob("test_latch_*_contract.py")))])
    result = pytest.main([
        "--disable-plugin-autoload", "-p", "no:cacheprovider", "-q",
        "--basetemp", str(temporary_root / "pytest"),
        "--rootdir", str(AGP), "--confcutdir", str(AGP), *selected,
    ], plugins=[sys.modules[__name__]])
    unchanged = before == source_hashes()
    print("OFFLINE_RESULT " + json.dumps({
        "tests": COUNTS, "blocked_attempts": BLOCKED,
        "agp_sources_unchanged": unchanged,
    }, sort_keys=True))
    return result if result else int(bool(BLOCKED) or not unchanged)


if __name__ == "__main__":
    raise SystemExit(main())
