"""Run the AGP suite in a synthetic home with networking and live tools denied.

Requires an environment with pytest installed. Does not install dependencies,
read live configuration, invoke services, or send notifications.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


GUARD = '''import os, socket, sys
def deny(*args, **kwargs):
    raise AssertionError("Network forbidden during offline verification")
socket.socket.connect = deny
socket.socket.connect_ex = deny
socket.create_connection = deny
original_home = os.environ["AGP_VERIFY_ORIGINAL_HOME"]
venv = sys.prefix.rstrip(os.sep) + os.sep
def guard(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
        deny()
    if event == "subprocess.Popen":
        command = args[1]
        if not isinstance(command, (list, tuple)) or not command or command[0] != sys.executable:
            raise AssertionError("Only reviewed Python test children are permitted")
    if event == "sqlite3.connect":
        if original_home in str(args[0]):
            raise AssertionError("Production database access forbidden")
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = os.fsdecode(args[0])
        if path.startswith(original_home + os.sep) and not path.startswith(venv):
            raise AssertionError("Production files forbidden")
sys.addaudithook(guard)
'''

CHILD = '''import json, pathlib, platform, sys, time
import pytest
class Results:
    collected = 0
    counts = {}
    def pytest_collection_finish(self, session):
        self.collected = len(session.items)
    def pytest_runtest_logreport(self, report):
        if report.when == "call" or report.failed:
            self.counts[report.outcome] = self.counts.get(report.outcome, 0) + 1
results = Results()
started = time.monotonic()
code = pytest.main(["-q", "-p", "no:cacheprovider", "--basetemp", "test-tmp", "tests"], plugins=[results])
pathlib.Path("results.json").write_text(json.dumps({
    "python": platform.python_version(), "pytest": pytest.__version__,
    "collected": results.collected, "counts": results.counts,
    "exit_code": code, "seconds": time.monotonic() - started,
    "network_forbidden": True, "synthetic_home": True,
    "production_configuration_used": False, "production_database_used": False
}, indent=2) + "\\n")
raise SystemExit(code)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        parser.error('Output already exists; preserve prior verification evidence')
    source = Path(__file__).resolve().parents[1]
    original_home = str(Path.home().resolve())
    if original_home == os.sep:
        parser.error('Refusing an unbounded original home')
    with tempfile.TemporaryDirectory(prefix='phase8p-offline-') as temporary:
        root = Path(temporary)
        for name in ('agp_race_agent', 'tests', 'scripts'):
            shutil.copytree(source / name, root / name,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        shutil.copyfile(source / 'config.example.toml', root / 'config.toml')
        shutil.copyfile(source / 'pyproject.toml', root / 'pyproject.toml')
        home = root / 'synthetic-home'
        (home / '.codex').mkdir(parents=True)
        (home / '.codex/config.toml').write_text(
            '[mcp_servers.agp-track-race]\ncommand = "offline-mcp-forbidden"\nargs = []\nenv = {}\n')
        guard = root / 'guard'
        guard.mkdir()
        (guard / 'sitecustomize.py').write_text(GUARD)
        (root / 'temporary').mkdir()
        env = {'HOME': str(home), 'PATH': '/usr/bin:/bin',
               'TMPDIR': str(root / 'temporary'),
               'PYTHONDONTWRITEBYTECODE': '1', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1',
               'PYTHONPATH': str(guard) + os.pathsep + str(root),
               'AGP_VERIFY_ORIGINAL_HOME': original_home}
        result = subprocess.run([sys.executable, '-B', '-c', CHILD], cwd=root, env=env)
        evidence = root / 'results.json'
        if evidence.exists():
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open('x') as stream:
                stream.write(evidence.read_text())
        return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
