# Update verification

Run the normal test suite from the repository root:

```sh
python3 -m pytest -q
```

The update tests cover dirty tracked files, concurrent updates, a service belonging
to another checkout, changed runtime files during preparation, candidate config
failure, build failure, replacement failure, successful rollback, failed rollback,
and explicit recovery. Readiness tests reject snapshots and validator cycles from
before replacement. Lifecycle tests check pinned project selection and prevent a
normal start from rebuilding retained images.

An opt-in test runs actual Git checkouts, Docker builds, six fixture containers,
and a named volume:

```sh
TG_UPDATE_DOCKER_TEST=1 python3 -m pytest -q -s tests/test_update_docker.py
```

It installs a working fixture release, attempts a broken one, verifies automatic
rollback, and stops/starts the recovered containers using saved image pins. It
checks the installed commit, running image contents, unchanged `.env`, volume
identity, and retained synthetic data. Its randomly named Compose project and
volume are removed in `finally`. Test image tags remain available for inspection.

The fixture replaces systemd actions with equivalent container operations and
uses a fixture readiness script. It does not exercise host reboot, production
systemd supervision, client routing, DNS, or actual proxy traffic. Those checks
belong to the lab acceptance run, not to the fixture result.

For a nested Docker runner that cannot load an AppArmor profile, the test supports
`TG_DOCKER_NESTED=1`; this changes only the synthetic fixture containers.

## Lab acceptance

Use a clean tracked checkout and retain the operator's local edits before any
installation. Establish readiness and a successful client request before testing.
Then verify a working update and a deliberately broken release, inspect both
command exit status and rollback output, compare config files privately, and
repeat the client request. Reboot only the designated test VM and verify the
recovered version starts with the saved images and data.

Use compatible data formats. Code/image rollback preserves volumes; it cannot
reverse application changes already written inside them.
