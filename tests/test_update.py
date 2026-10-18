"""Exercise update transactions without touching host services or network rules."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("tg_update", PROJECT / "scripts/tg_update.py")
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


class UserError(Exception):
    pass


@pytest.fixture
def transaction(tmp_path, monkeypatch):
    runtime = tmp_path / ".env"
    runtime.write_text("TEST=value\n")
    unit = tmp_path / "unit"
    unit.write_text("WorkingDirectory=" + str(tmp_path) + "\n")
    ctl = SimpleNamespace(ROOT=tmp_path, SYSTEMD_UNIT=unit, RUNTIME_FILES={".env": 0o600},
                          UserError=UserError, load_and_validate=lambda: None,
                          prepare_service=lambda timeout: None)
    events = []
    state = {"commit": "a" * 40, "dirty": False, "health_failures": 0}
    config = {"name": "existing-project", "services": {"redis": {"image": "redis:test"}},
              "volumes": {"data": {"name": "existing-project_data"}}}
    volumes = {"redis:/data": "existing-project_data"}

    def git(root, *args):
        events.append(("git", args))
        if args[0] == "status":
            return " M changed.py" if state["dirty"] else ""
        if args[0] == "rev-parse":
            return state["commit"] if args[1] == "HEAD" else "b" * 40
        if args[0] == "checkout":
            state["commit"] = args[-1]
        return ""

    def execute(command, root, **kwargs):
        events.append(("execute", command))
        if "config" in command:
            return json.dumps(config)
        return ""

    def health(root, timeout, since=None):
        events.append(("health", since))
        if since is not None and state["health_failures"]:
            state["health_failures"] -= 1
            raise updater.UpdateFailure("synthetic readiness failure")

    def candidate(root, stage, project, commit, previous, runtime_names):
        events.append(("candidate", commit))
        stage.mkdir()
        if state.get("prepare_fail"):
            raise updater.UpdateFailure("synthetic build failure")
        return {"name": project, "services": {"redis": {"image": "new-image", "pull_policy": "never"}}}

    monkeypatch.setattr(updater, "git", git)
    monkeypatch.setattr(updater, "execute", execute)
    monkeypatch.setattr(updater, "health", health)
    monkeypatch.setattr(updater, "candidate", candidate)
    monkeypatch.setattr(updater, "current_installation", lambda root: (config["name"], {"redis": "sha256:old"}, volumes))
    monkeypatch.setattr(updater, "service", lambda root, action, timeout: events.append(("service", action)))
    args = argparse.Namespace(timeout=30, recover=False, revision="main")
    return ctl, args, state, events


def test_success_pins_images_and_preserves_runtime(transaction):
    ctl, args, state, events = transaction
    before = (ctl.ROOT / ".env").read_bytes()
    updater.update(args, ctl)
    assert state["commit"] == "b" * 40
    assert (ctl.ROOT / ".env").read_bytes() == before
    journal = json.loads((ctl.ROOT / "runtime/update/transaction.json").read_text())
    assert journal["phase"] == "complete"
    assert events.index(("candidate", "b" * 40)) < events.index(("service", "stop"))
    assert json.loads((ctl.ROOT / "runtime/update/images.json").read_text())["name"] == "existing-project"
    assert any(kind == "health" and value is not None for kind, value in events)


def test_dirty_code_stops_before_fetch_or_containers(transaction):
    ctl, args, state, events = transaction
    state["dirty"] = True
    with pytest.raises(UserError, match="changed.py"):
        updater.update(args, ctl)
    assert not any(kind in {"service", "candidate", "execute"} for kind, _ in events)


def test_build_failure_does_not_stop_old_service(transaction):
    ctl, args, state, events = transaction
    state["prepare_fail"] = True
    with pytest.raises(UserError, match="build failure"):
        updater.update(args, ctl)
    assert state["commit"] == "a" * 40
    assert not any(kind == "service" for kind, _ in events)
    assert not (ctl.ROOT / "runtime/update/transaction.json").exists()


def test_runtime_change_during_preparation_aborts_before_stop(transaction, monkeypatch):
    ctl, args, state, events = transaction
    prepare = updater.candidate
    def changed_runtime(*arguments):
        result = prepare(*arguments)
        (ctl.ROOT / ".env").write_text("TEST=edited-by-user\n")
        return result
    monkeypatch.setattr(updater, "candidate", changed_runtime)
    with pytest.raises(UserError, match="runtime files changed during preparation"):
        updater.update(args, ctl)
    assert not any(kind == "service" for kind, _ in events)
    assert (ctl.ROOT / ".env").read_text() == "TEST=edited-by-user\n"


def test_other_checkout_service_is_not_stopped(transaction):
    ctl, args, state, events = transaction
    ctl.SYSTEMD_UNIT.write_text("WorkingDirectory=/somewhere/else\n")
    with pytest.raises(UserError, match="another checkout"):
        updater.update(args, ctl)
    assert not any(kind == "service" for kind, _ in events)


def test_concurrent_update_is_refused(transaction):
    import fcntl
    ctl, args, state, events = transaction
    directory = ctl.ROOT / "runtime/update"
    directory.mkdir(parents=True)
    with open(directory / "lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(UserError, match="already running"):
            updater.update(args, ctl)
    assert not events


def test_failed_readiness_restores_code_images_and_checks_again(transaction, capsys):
    ctl, args, state, events = transaction
    state["health_failures"] = 1
    with pytest.raises(UserError, match="previous version restored"):
        updater.update(args, ctl)
    assert state["commit"] == "a" * 40
    journal = json.loads((ctl.ROOT / "runtime/update/transaction.json").read_text())
    assert journal["phase"] == "rolled_back"
    pins = json.loads((ctl.ROOT / "runtime/update/images.json").read_text())
    assert pins == journal["previous_images"]
    assert "ROLLBACK PASS" in capsys.readouterr().out
    assert len([x for x in events if x[0] == "health" and x[1] is not None]) == 2


def test_failed_recovery_retains_journal_for_retry(transaction, capsys):
    ctl, args, state, events = transaction
    state["health_failures"] = 2
    with pytest.raises(UserError, match="automatic recovery failed"):
        updater.update(args, ctl)
    assert "ROLLBACK PASS" not in capsys.readouterr().out
    path = ctl.ROOT / "runtime/update/transaction.json"
    assert json.loads(path.read_text())["phase"] == "switching"
    args.recover = True
    updater.update(args, ctl)
    assert json.loads(path.read_text())["phase"] == "rolled_back"


def test_interrupted_update_requires_explicit_recovery(transaction):
    ctl, args, state, events = transaction
    directory = ctl.ROOT / "runtime/update"
    directory.mkdir(parents=True)
    updater.atomic_json(directory / "transaction.json", {"phase": "switching"})
    with pytest.raises(UserError, match="--recover"):
        updater.update(args, ctl)
    assert not any(kind == "service" for kind, _ in events)


def test_new_required_config_failure_precedes_build(tmp_path, monkeypatch):
    root, stage = tmp_path / "root", tmp_path / "candidate"
    root.mkdir()
    (root / ".env").write_text("OLD=value\n")
    events = []

    def git(root, *args):
        if args[0] == "ls-tree":
            return ""
        (stage / "scripts").mkdir(parents=True)
        (stage / "scripts/tgctl.py").write_text("UPDATE_API_VERSION = 1\n")

    def execute(command, root, **kwargs):
        events.append(command)
        if "check" in command:
            raise updater.UpdateFailure("missing NEW_REQUIRED_SETTING")
        return ""

    monkeypatch.setattr(updater, "git", git)
    monkeypatch.setattr(updater, "execute", execute)
    with pytest.raises(updater.UpdateFailure, match="NEW_REQUIRED_SETTING"):
        updater.candidate(root, stage, "existing", "b" * 40, {}, [".env"])
    assert len(events) == 1
    assert (root / ".env").read_text() == "OLD=value\n"


def test_named_volume_reassignment_is_detected():
    before = {"services": {"redis": {"volumes": [{"type": "volume", "source": "data", "target": "/data"}]}}}
    after = {"services": {"redis": {"volumes": [{"type": "volume", "source": "empty", "target": "/data"}]}}}
    assert updater.volume_layout(before) != updater.volume_layout(after)


@pytest.mark.parametrize("old_cycle", [False, True])
def test_old_snapshot_cannot_pass_post_update_readiness(monkeypatch, old_cycle):
    sys_path = str(PROJECT / "deploy")
    monkeypatch.syspath_prepend(sys_path)
    import readiness
    services = sorted(readiness.SERVICES)
    def output(command):
        if "ps" in command:
            return "\n".join(json.dumps({"Service": name, "State": "running", "Health": "healthy"}) for name in services)
        return json.dumps({"generated_at": 999 if old_cycle else 995,
                           "validator_cycle": {"completed_at": 995}, "entries": [{"address": "test"}]})
    monkeypatch.setattr(readiness.time, "time", lambda: 1000)
    values = {"FETCH_INTERVAL_SECONDS": "15", "VALIDATION_INTERVAL_SECONDS": "15", "RANK_INTERVAL_SECONDS": "5"}
    with pytest.raises(RuntimeError, match="predates replacement"):
        readiness.check(["docker", "compose"], output, values, since=998)
