"""Transactional updates for the tgctl single-host installation.

Candidate code/config/images are prepared before stopping the service. Only
images are pinned in the persistent Compose override: runtime config still comes
from the user's files. The journal survives process/host failure for --recover.
"""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


class UpdateFailure(Exception):
    pass


def execute(command, root, *, timeout=120, visible=False):
    result = subprocess.run([str(part) for part in command], cwd=root, text=True,
                            capture_output=not visible, timeout=timeout)
    if result.returncode:
        # Git/Compose diagnostics may contain URLs/environment. Don't echo them.
        raise UpdateFailure("command failed: " + " ".join(str(p) for p in command[:3]))
    return result.stdout or ""


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        os.chmod(temporary, 0o600)
        json.dump(value, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def git(root, *arguments):
    return execute(["git", "-c", "safe.directory=" + str(root), *arguments], root).strip()


def clean_checkout(root):
    changed = git(root, "status", "--porcelain", "--untracked-files=no")
    if changed:
        # Filenames only, never a diff (which could contain credentials).
        raise UpdateFailure("tracked files changed; save your changes first:\n" + changed)


def runtime_fingerprint(root, names):
    result = {}
    for name in names:
        path = root / name
        info = path.stat()
        result[name] = [hashlib.sha256(path.read_bytes()).hexdigest(),
                        info.st_uid, info.st_gid, info.st_mode]
    return result


def compose(root, *arguments):
    command = ["docker", "compose"]
    override = root / "runtime/update/images.json"
    if override.exists():
        project = json.loads(override.read_text())["name"]
        command += ["--project-name", project, "-f", str(root / "docker-compose.yml"), "-f", str(override)]
    return command + list(arguments)


def container_image(root, row):
    """Resolve an addressable image, including Docker's containerd image store.

    Container.Image can be a config digest, while image inspect/tag addresses the
    containing OCI index. Verify the immutable index's platform manifest against
    the running container before retaining it; a moved tag alone is not evidence.
    """
    service_name = row["Config"]["Labels"]["com.docker.compose.service"]
    descriptor = row.get("ImageManifestDescriptor") or {}
    expected = descriptor.get("digest")
    platform_data = descriptor.get("platform") or {}
    platform = "/".join(platform_data.get(key, "") for key in ("os", "architecture"))
    if platform_data.get("variant"):
        platform += "/" + platform_data["variant"]

    def inspect(reference, selected_platform=None):
        command = ["docker", "image", "inspect"]
        if selected_platform:
            command += ["--platform", selected_platform]
        return json.loads(execute(command + [reference], root))[0]

    try:
        image = inspect(row["Image"])
        return image["Id"]
    except UpdateFailure:
        pass

    if expected and all(platform_data.get(key) for key in ("os", "architecture")):
        checked = set()

        def matching(reference):
            if not reference or reference in checked:
                return None
            checked.add(reference)
            try:
                image_id = inspect(reference)["Id"]
                selected = inspect(image_id, platform)
                digest = (selected.get("Descriptor") or {}).get("digest", selected["Id"])
                return image_id if digest == expected else None
            except UpdateFailure:
                return None

        image_id = matching(row["Config"].get("Image"))
        if image_id:
            return image_id
        # A rebuilt tag can point elsewhere; look for the original local index.
        for reference in execute(["docker", "image", "ls", "--all", "--quiet", "--no-trunc"], root).split():
            image_id = matching(reference)
            if image_id:
                return image_id
    raise UpdateFailure("cannot retain the running image for " + service_name
                        + "; no matching local image found; containers have not been replaced")


def current_installation(root):
    config = json.loads(execute(compose(root, "config", "--format", "json"), root))
    identifiers = execute(compose(root, "ps", "--all", "--quiet"), root).split()
    if not identifiers:
        raise UpdateFailure("no installed containers; run tgctl.py up first")
    rows = json.loads(execute(["docker", "inspect", *identifiers], root))
    services = {}
    volumes = {}
    for row in rows:
        labels = row["Config"]["Labels"]
        if labels.get("com.docker.compose.oneoff", "False").lower() == "true":
            continue
        service = labels["com.docker.compose.service"]
        if service in services or not row["State"]["Running"]:
            raise UpdateFailure("expected one running container per service: " + service)
        services[service] = container_image(root, row)
        for mount in row["Mounts"]:
            if mount["Type"] == "volume":
                volumes[service + ":" + mount["Destination"]] = mount["Name"]
    if set(services) != set(config["services"]):
        raise UpdateFailure("installed services differ from Compose configuration")
    return config["name"], services, volumes


def pin_images(root, project, images):
    # Tags protect old images from ordinary dangling-image pruning.
    prefix = hashlib.sha256(str(root).encode()).hexdigest()[:12]
    pins = {}
    for service, identifier in images.items():
        tag = f"tg-retained-{prefix}-{service}:{identifier.split(':')[-1]}"
        execute(["docker", "tag", identifier, tag], root)
        pins[service] = {"image": tag, "pull_policy": "never"}
    return {"name": project, "services": pins}


def health(root, timeout, since=None):
    command = [sys.executable, str(root / "scripts/tgctl.py"), "ready", "--timeout", str(timeout)]
    if since is not None:
        command += ["--since", str(since)]
    execute(command, root, timeout=timeout + 90, visible=True)


def service(root, action, timeout):
    execute(["systemctl", action, "transparent-gateway.service"], root, timeout=timeout + 90)


def restore(root, directory, journal, timeout):
    print("ROLLBACK restoring_previous_version=1", flush=True)
    # Stop directly too: the unit can be inactive after a partial failed start.
    execute(compose(root, "stop", "--timeout", "20"), root, timeout=90)
    service(root, "stop", timeout)
    git(root, "checkout", "--detach", journal["previous_commit"])
    atomic_json(directory / "images.json", journal["previous_images"])
    since = time.time()
    execute(compose(root, "up", "--detach", "--no-build", "--pull", "never"), root,
            timeout=timeout + 90, visible=True)
    service(root, "start", timeout)
    health(root, timeout, since)
    _, _, volumes = current_installation(root)
    if volumes != journal["volumes"]:
        raise UpdateFailure("recovery mounted unexpected volumes")
    journal["phase"] = "rolled_back"
    atomic_json(directory / "transaction.json", journal)
    print("ROLLBACK PASS previous_version_running=1", flush=True)


def candidate(root, stage, project, commit, previous, runtime_names):
    git(root, "worktree", "add", "--detach", str(stage), commit)
    script = stage / "scripts/tgctl.py"
    if not script.exists() or "UPDATE_API_VERSION = 1" not in script.read_text():
        raise UpdateFailure("target version does not support the update format")
    for name in runtime_names:
        source, destination = root / name, stage / name
        if git(root, "ls-tree", "--name-only", commit, "--", name):
            raise UpdateFailure("target tracks a runtime file: " + name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        info = source.stat()
        os.chown(destination, info.st_uid, info.st_gid)
    execute([sys.executable, str(script), "check"], stage, visible=True)
    command = ["docker", "compose", "--project-name", project]
    config = json.loads(execute(command + ["config", "--format", "json"], stage))
    if set(config["services"]) != set(previous):
        raise UpdateFailure("this updater cannot add or remove services")
    pins = {}
    prefix = hashlib.sha256(str(root).encode()).hexdigest()[:12]
    for name, spec in config["services"].items():
        if spec.get("build"):
            image = f"tg-release-{prefix}-{name}:{commit}"
        else:
            image = spec["image"]
            execute(["docker", "pull", image], root, timeout=900, visible=True)
        pins[name] = {"image": image}
    override = stage / "update-build.json"
    atomic_json(override, {"services": pins})
    execute(command + ["-f", str(stage / "docker-compose.yml"), "-f", str(override), "build"],
            stage, timeout=1800, visible=True)
    images = {}
    for name, pin in pins.items():
        images[name] = execute(["docker", "image", "inspect", "--format", "{{.Id}}", pin["image"]], root).strip()
    return pin_images(root, project, images)


def volume_layout(config):
    return {name: sorted((v["source"], v["target"]) for v in spec.get("volumes", [])
                         if v["type"] == "volume")
            for name, spec in config["services"].items()}


def update(args, ctl):
    root = ctl.ROOT
    if args.timeout <= 0:
        raise ctl.UserError("--timeout must be positive")
    directory = root / "runtime/update"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    lock = open(directory / "lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        lock.close()
        raise ctl.UserError("another update is already running") from error
    stage = None
    journal = None
    switched = False
    try:
        clean_checkout(root)
        if any(os.environ.get(name) for name in ("COMPOSE_FILE", "COMPOSE_PROFILES")):
            raise UpdateFailure("custom Compose files/profiles are not supported by this updater")
        if any((root / name).exists() for name in ("compose.override.yml", "compose.override.yaml",
                "docker-compose.override.yml", "docker-compose.override.yaml")):
            raise UpdateFailure("custom Compose override files are not supported by this updater")
        if not ctl.SYSTEMD_UNIT.exists():
            raise UpdateFailure("install the systemd service with tgctl.py up before updating")
        if "WorkingDirectory=" + str(root.resolve()) not in ctl.SYSTEMD_UNIT.read_text().splitlines():
            raise UpdateFailure("systemd service belongs to another checkout; run tgctl.py up here first")
        journal_path = directory / "transaction.json"
        if journal_path.exists():
            old = json.loads(journal_path.read_text())
            if old["phase"] not in {"complete", "rolled_back"}:
                if not args.recover:
                    raise UpdateFailure("interrupted update found; run tgctl.py update --recover")
                restore(root, directory, old, args.timeout)
                return
        if args.recover:
            raise UpdateFailure("no interrupted update to recover")
        runtime = ctl.load_and_validate()
        if runtime and any(runtime[0].get(name) for name in ("COMPOSE_FILE", "COMPOSE_PROFILES")):
            raise UpdateFailure("custom Compose files/profiles in .env are not supported by this updater")
        health(root, args.timeout)
        project, images, volumes = current_installation(root)
        previous = git(root, "rev-parse", "HEAD")
        if args.revision.startswith("-") or not args.revision.strip():
            raise UpdateFailure("invalid revision")
        print("UPDATE preparing=1", flush=True)
        git(root, "fetch", "--no-tags", "origin", args.revision)
        target = git(root, "rev-parse", "--verify", "FETCH_HEAD^{commit}")
        print(f"UPDATE from={previous[:12]} to={target[:12]}", flush=True)
        if target == previous:
            print("UPDATE unchanged=1 commit=" + target, flush=True)
            return
        baseline = runtime_fingerprint(root, ctl.RUNTIME_FILES)
        retained = pin_images(root, project, images)
        # Put temporary credentials under the private runtime directory.
        stage = Path(tempfile.mkdtemp(prefix="candidate-", dir=directory))
        stage.rmdir()
        print("UPDATE phase=prepare_images current_gateway_running=1", flush=True)
        pins = candidate(root, stage, project, target, images, ctl.RUNTIME_FILES)
        clean_checkout(root)
        if runtime_fingerprint(root, ctl.RUNTIME_FILES) != baseline:
            raise UpdateFailure("runtime files changed during preparation; retry the update")
        # A schema-changing release requires a separate migration/backup design.
        old_config = json.loads(execute(["docker", "compose", "--project-name", project,
                                         "-f", str(root / "docker-compose.yml"), "config", "--format", "json"], root))
        new_config = json.loads(execute(["docker", "compose", "--project-name", project, "config", "--format", "json"], stage))
        if (old_config.get("volumes", {}) != new_config.get("volumes", {})
                or volume_layout(old_config) != volume_layout(new_config)):
            raise UpdateFailure("volume definitions changed; automatic update is not supported")
        if old_config["services"]["redis"] != new_config["services"]["redis"]:
            raise UpdateFailure("Redis configuration changed; a separate data migration is required")
        journal = {"phase": "switching", "previous_commit": previous,
                   "target_commit": target, "previous_images": retained,
                   "volumes": volumes}
        atomic_json(journal_path, journal)
        switched = True
        # ExecStop must use the pinned project too, including after a reboot.
        ctl.prepare_service(args.timeout)
        print("UPDATE phase=replace_containers", flush=True)
        service(root, "stop", args.timeout)
        git(root, "checkout", "--detach", target)
        atomic_json(directory / "images.json", pins)
        since = time.time()
        execute(compose(root, "up", "--detach", "--no-build", "--pull", "never"), root,
                timeout=args.timeout + 90, visible=True)
        service(root, "start", args.timeout)
        print("UPDATE phase=check_readiness", flush=True)
        health(root, args.timeout, since)
        _, _, after_volumes = current_installation(root)
        if after_volumes != volumes or runtime_fingerprint(root, ctl.RUNTIME_FILES) != baseline:
            raise UpdateFailure("runtime files or mounted volumes changed")
        journal["phase"] = "complete"
        atomic_json(journal_path, journal)
        print("UPDATE PASS commit=" + target + " runtime_preserved=1", flush=True)
    except (Exception, KeyboardInterrupt) as error:
        if switched:
            try:
                restore(root, directory, journal, args.timeout)
            except (Exception, KeyboardInterrupt) as recovery_error:
                raise ctl.UserError("update failed; automatic recovery failed; run update --recover: "
                                    + str(recovery_error)) from error
            raise ctl.UserError("update failed; previous version restored: " + str(error)) from error
        raise ctl.UserError(str(error)) from error
    finally:
        if stage is not None:
            # Only this invocation's private worktree; no runtime files are removed.
            try:
                git(root, "worktree", "remove", "--force", str(stage))
            except (UpdateFailure, subprocess.TimeoutExpired):
                pass
        lock.close()
