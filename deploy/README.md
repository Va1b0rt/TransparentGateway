# Stage 3: version-pinned deployment and local readiness

Run on the Debian13 gateway host provisioned by stage2. `deploy.py` requires root,
a full40-character commit SHA and an explicit HTTPS Git repository or absolute
local repository path. A branch name or implicit latest main is rejected. Use a
trusted copy of the deployment tool from the version you intend to deploy.

Canonical runtime files live outside Git in `/etc/transparent-gateway/runtime`:

| Path | Owner | Mode |
| --- | --- | --- |
| .env | root:root | 0600 |
| inventory/connectors.json | root:65532 | 0640 |
| inventory/upstreams.jsonl | root:65532 | 0640 |
| secrets/proxy-credentials.json | root:65532 | 0640 |

The `.env` format uses literal, unquoted `KEY=value` lines; duplicate keys and
interpolation are rejected. JSON inventory uses canonical host/port/protocol fields.
The empty credential store `{}` is valid for unauthenticated endpoints. Secrets
delivery and provider-token file support remain stage4 work. Do not put production
credentials into the lab examples or cloud-init.

The tool verifies the host's declared3 NICs and default route, requires CLIENT_CIDR
to match the client interface and exclude management, validates inventory/credential
references, fetches/checks out the requested commit, and atomically reconciles
ignored runtime files into `/opt/transparent-gateway`. It refuses tracked local
changes. The canonical files remain the source of truth. Unchanged file contents,
permissions and owner preserve the inode and modification time.

Compose runs with an explicit project, file, directory and env file in a controlled
process environment. `docker compose config --quiet` runs before build/up. Builds
reuse cached layers. `up --wait` preserves named volumes; the script never runs
`down`, removes volumes or forces container recreation. A deployment lock prevents
concurrent runs. The tool reports changed config count and created/unchanged
container IDs and writes a root-only deployment record outside the checkout.

Failures return a nonzero code. Subprocess stderr is deliberately not copied into
journal because it can contain interpolated config. An operator may inspect the
specific failed command privately; do not paste expanded Compose config into logs.
This stage has no automatic rollback; a failed new version may have started some
services. The previous recorded success is not evidence the current run succeeded.

## Local lab used for the article

`lab/compose.yml` provides a private proxy172.30.40.11:3128 and echo172.30.40.10:8000
on a Docker bridge, with no published host ports. The proxy allows only that echo
destination. This tests protocol negotiation, pipeline publication and relay use;
it cannot prove public-exit anonymity, DNS or client isolation. The fixture code
is test infrastructure, not a production forward proxy or public echo service.

On VM214, copy this committed source to `/var/lib/transparent-gateway/source` (a
Git bundle works while the branch is unpublished), then run:

```sh
sudo python3 /var/lib/transparent-gateway/source/deploy/lab/prepare-config.py
sudo docker compose -p tg-lab -f /var/lib/transparent-gateway/source/deploy/lab/compose.yml up -d
sudo python3 /var/lib/transparent-gateway/source/deploy/deploy.py \
  --source /var/lib/transparent-gateway/source --commit FULL_COMMIT_SHA
```

The fixture address space must be unused. Its config explicitly admits unknown
ASNs because private echo peer addresses do not have public ASNs. Do not copy this
lab-only ranking choice into a production deployment. ASN download and checksum
verification are still real; no invented ASN database is used.

## Acceptance boundary

After Compose becomes running/healthy, local readiness requires fresh, nonempty
raw/validated/reserve/active snapshots, recent validator input, compatible active
membership, and a new CONNECT through the gateway relay followed by a real echo
request. The echo's peer must match an active candidate's validated exit IP.

The final state is explicitly **LOCAL_READY**, with **client/DNS/public checks
pending**. This is not full production readiness. The stage3 gate currently accepts
an HTTP echo only; stage6/7 will add a client VM, DNS paths, HTTPS/public echo and
full readiness. It must not be relabeled simply READY before those checks exist.

Repeat the exact same deployment command. Acceptance: config_files_changed0,
containers_created_or_replaced0, containers_unchanged6, fresh snapshots and relay
echo still pass. Additionally verify Redis run_id/data and gateway rule counts
before/after; normal background pipeline updates are expected.

References: [Compose up and volume preservation](https://docs.docker.com/reference/cli/docker/compose/up/),
[Compose cached builds](https://docs.docker.com/reference/cli/docker/compose/build/).
