"""Opt-in real Docker/Git transaction test; no host systemd or network rules.

TG_UPDATE_DOCKER_TEST=1 python3 -m pytest -q -s tests/test_update_docker.py
The six small fixture services test replacement/rollback/volumes, not proxying.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.skipif(os.environ.get("TG_UPDATE_DOCKER_TEST") != "1",
                                reason="opt-in Docker integration test")
PROJECT = Path(__file__).resolve().parents[1]


def command(root, *args):
    return subprocess.check_output(list(args), cwd=root, text=True).strip()


def test_real_images_git_rollback_and_persistent_volume(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("docker_test_updater", PROJECT / "scripts/tg_update.py")
    updater = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(updater)
    origin, root = tmp_path / "origin", tmp_path / "installed"
    origin.mkdir()
    command(origin, "git", "init")
    command(origin, "git", "symbolic-ref", "HEAD", "refs/heads/main")
    command(origin, "git", "config", "user.email", "fixture@example.invalid")
    command(origin, "git", "config", "user.name", "Update fixture")
    (origin / ".gitignore").write_text(".env\nruntime/\n")
    (origin / "version.txt").write_text("first")
    (origin / "Dockerfile").write_text("FROM python:3.12-slim\nCOPY version.txt /version.txt\nCOPY app.py /app.py\nCMD [\"python\", \"/app.py\"]\n")
    (origin / "app.py").write_text("import os,time\nfrom pathlib import Path\n"
        "if os.environ.get('ROLE')=='gateway' and Path('/version.txt').read_text()=='broken': raise SystemExit(1)\n"
        "Path('/tmp/alive').touch()\ntime.sleep(3600)\n")
    services = {"redis": {"image": "python:3.12-slim", "command": ["python", "-c",
        "from pathlib import Path; import time; p=Path('/data/sentinel'); p.write_text(p.read_text() if p.exists() else 'retained'); time.sleep(3600)"],
        "volumes": ["data:/data"]}}
    for name in ("fetcher", "validator", "ranker", "asn-updater", "gateway"):
        services[name] = {"build": {"context": "."}, "environment": {"ROLE": name},
            "healthcheck": {"test": ["CMD", "python", "-c", "from pathlib import Path; assert Path('/tmp/alive').exists()"],
                            "interval": "1s", "timeout": "1s", "retries": 3}}
    for service in services.values():
        service["stop_grace_period"] = "1s"
    if os.environ.get("TG_DOCKER_NESTED") == "1":
        # This runner is itself a container without a loadable AppArmor profile.
        for service in services.values():
            service["security_opt"] = ["apparmor=unconfined"]
    project = "tg-update-test-" + hashlib.sha256(str(tmp_path).encode()).hexdigest()[:12]
    (origin / "docker-compose.yml").write_text(json.dumps({"name": project, "services": services, "volumes": {"data": {}}}))
    scripts = origin / "scripts"
    scripts.mkdir()
    (scripts / "tgctl.py").write_text('''import json,subprocess,sys,time
from pathlib import Path
UPDATE_API_VERSION = 1
root=Path(__file__).resolve().parents[1]
command=['docker','compose']
pins=root/'runtime/update/images.json'
if pins.exists(): command += ['--project-name',json.loads(pins.read_text())['name'],'-f',str(root/'docker-compose.yml'),'-f',str(pins)]
if sys.argv[1]=='check':
    assert (root/'.env').read_text()=='FIXTURE=1\\n'
    raise SystemExit(0)
deadline=time.monotonic()+int(sys.argv[sys.argv.index('--timeout')+1])
while time.monotonic()<deadline:
    raw=subprocess.check_output(command+['ps','--all','--format','json'],cwd=root,text=True)
    rows=[json.loads(line) for line in raw.splitlines() if line]
    if len(rows)==1 and isinstance(rows[0],list): rows=rows[0]
    if len(rows)==6 and all(r['State']=='running' and r.get('Health','') in ('','healthy') for r in rows): raise SystemExit(0)
    time.sleep(.5)
raise SystemExit(1)
''')
    command(origin, "git", "add", ".")
    command(origin, "git", "commit", "-m", "first fixture release")
    command(tmp_path, "git", "clone", str(origin), str(root))
    (root / ".env").write_text("FIXTURE=1\n")
    unit = tmp_path / "fake.service"
    unit.write_text("WorkingDirectory=" + str(root) + "\n")
    class UserError(Exception):
        pass
    ctl = SimpleNamespace(ROOT=root, SYSTEMD_UNIT=unit, RUNTIME_FILES={".env": 0o600},
                          UserError=UserError, load_and_validate=lambda: None,
                          prepare_service=lambda timeout: None)
    def service(root, action, timeout):
        # Equivalent container action, intentionally no host systemd mutation.
        args = ["stop"] if action == "stop" else ["up", "--detach", "--no-build", "--pull", "never"]
        updater.execute(updater.compose(root, *args), root, timeout=90)
    monkeypatch.setattr(updater, "service", service)
    try:
        updater.execute(updater.compose(root, "up", "--detach", "--build"), root, timeout=180, visible=True)
        updater.health(root, 20)
        _, _, volumes = updater.current_installation(root)
        (origin / "version.txt").write_text("second")
        command(origin, "git", "commit", "-am", "working update")
        args = argparse.Namespace(timeout=8, revision="main", recover=False)
        updater.update(args, ctl)
        second = command(origin, "git", "rev-parse", "HEAD")
        assert command(root, "git", "rev-parse", "HEAD") == second
        assert updater.current_installation(root)[2] == volumes
        assert updater.execute(updater.compose(root, "exec", "-T", "gateway", "cat", "/version.txt"), root).strip() == "second"
        (origin / "version.txt").write_text("broken")
        command(origin, "git", "commit", "-am", "deliberately broken gateway")
        with pytest.raises(UserError, match="previous version restored"):
            updater.update(args, ctl)
        assert command(root, "git", "rev-parse", "HEAD") == second
        assert updater.current_installation(root)[2] == volumes
        assert updater.execute(updater.compose(root, "exec", "-T", "redis", "cat", "/data/sentinel"), root).strip() == "retained"
        # Cold container start with the saved pins, as systemd does on boot.
        service(root, "stop", 20)
        service(root, "start", 20)
        updater.health(root, 20)
        assert updater.execute(updater.compose(root, "exec", "-T", "gateway", "cat", "/version.txt"), root).strip() == "second"
        assert (root / ".env").read_text() == "FIXTURE=1\n"
    finally:
        # Only this test's random temporary Compose project and synthetic data.
        updater.execute(updater.compose(root, "down", "--volumes", "--remove-orphans"), root, timeout=90)
