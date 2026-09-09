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
  --source /var/lib/transparent-gateway/source --commit FULL_COMMIT_SHA --lab-fixture
```

The fixture address space must be unused. Its config explicitly admits unknown
ASNs because private echo peer addresses do not have public ASNs. Do not copy this
lab-only ranking choice into a production deployment. ASN download and checksum
verification are still real; no invented ASN database is used.

The first deployment exposed Docker's inter-bridge isolation: the host could reach
the fixture, but the validator on the application bridge timed out. `--lab-fixture`
now reconciles the separate fixture project and applies a reviewed override that
adds the validator to that specific external network. Host packet-filter policy
is not relaxed to connect arbitrary Docker networks. Reuse this flag on subsequent
lab deployments; omitting it changes the intended validator network configuration.

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

### Stage 4: secrets over SSH

Prepare a complete runtime directory on the trusted operator host, owned by root
with mode 0700. It contains `.env` (0600), `inventory/connectors.json`,
`inventory/upstreams.jsonl`, `secrets/proxy-credentials.json` and
`provider-secrets/api-token` (0600 or 0640 on the operator). No secret values go
in command arguments, Git, cloud-init or Compose environment. `.env` carries
non-secret deployment settings; bearer tokens in `.env` are rejected. An empty
token file is allowed when no authenticated feed is active.

With the receiver source installed at the pinned deployment commit:

```sh
sudo python3 deploy/secret-transfer.py send \
  --host tgadmin@192.168.88.114 --directory /root/tg-runtime
```

SSH uses strict existing host-key verification and noninteractive authentication.
The receiver accepts only the five declared files, stages and validates the entire
set, then atomically replaces changed files under the deployment lock. It never
prints supplied values or exception contents. A power loss during replacement of
multiple files requires resending the same set; this is not a filesystem-wide
transaction. Delivery does not itself deploy or restart services: run `deploy.py`
with the intended full commit afterwards. A repeat preserves unchanged files.

On the VM, canonical directory permissions are 0700; `.env` is root:root 0600;
container-readable files are root:65532 0640 and their directories 0750. UID 65532
is the application's container identity; the privileged gateway can also read
its proxy store. Provider and proxy secrets are in separate directories mounted
read-only only into their consumers. Directory mounts make atomic file rotation
visible without retaining an obsolete file-bind inode. The lab proxy also mounts
the proxy store, exclusively as an owned authentication test fixture.

An authorised HTTPS feed can declare:

```json
{"type":"http_json_feed","name":"operator-provider",
 "url":"https://provider.example/feed",
 "token_file":"/run/provider-secrets/api-token"}
```

`token_env` is no longer supported. Migrate it to `token_file` and deliver the token
file before deploying this revision. The connector rereads the file for every
fetch, sends a bearer header, rejects secret-bearing URL forms and refuses
redirects. Proxy snapshots contain credential references; handshake code reads
the reloadable proxy store. Provider tokens never enter container environment.

For this lab, `verify-secrets.py` checks actual file permissions and consumer
readability, mount isolation, rejected missing/wrong/previous proxy passwords,
successful current authentication and a fresh relay echo. It scans current/previous
known test secret values and common base64 forms in tracked files, cloud-init,
journal, existing container logs/inspect and Redis string values. This bounded
scan is evidence for the test values, not a guarantee about arbitrary unknown
secrets or erased historical logs. The provider file is tested for isolation and
unit-tested header/rotation behavior; an actual provider API needs its own
operator-supplied credentials and is not claimed as exercised by the lab.

To generate this isolated lab's operator runtime from scratch (no production
credentials), use `sudo python3 deploy/lab/prepare-config.py --destination
/root/tg-runtime` on the operator host. The destination must not exist. It creates
random test proxy credentials and a dummy provider token without printing them;
the lab proxy reads the delivered store and checks Basic authentication. Transfer
that directory using the SSH command above, then deploy with `--lab-fixture`.

### Stage 5: systemd owns the application lifecycle

After deploying a full release (with images already built), install its unit:

```sh
sudo python3 /var/lib/transparent-gateway/source/deploy/install-service.py \
  --commit FULL_40_CHARACTER_SHA --lab-fixture
sudo python3 /var/lib/transparent-gateway/source/deploy/verify-boot.py --prepare
sudo systemctl reboot
# Reconnect after boot; do not start Docker or Compose manually:
sudo python3 /var/lib/transparent-gateway/source/deploy/verify-boot.py
```

`transparent-gateway.service` depends on Docker and orders after network-online.
The installer requires the image's enabled systemd-networkd wait-online service;
preflight also waits for the three exact declared NIC addresses and default route.
It checks the installed commit, clean tracked files, secret permissions and quiet
Compose validation. Service settings contain only commit/path/fixture flag, root0600.
The unit starts existing images with no build or pull. It sends systemd READY only
after local readiness, and records the boot ID and result. Full client/DNS/public
readiness remains a subsequent stage.

Docker restart policies are disabled for the application and lab fixture. A single
systemd supervisor checks container presence/running/health every 15 seconds; a
failure exits nonzero, systemd stops remaining containers and retries the stack.
Shutdown stops containers, preserving volumes. Docker stop/restart propagates to
the unit; enabling it for both multi-user and Docker startup brings it back when
Docker starts. Startup retries are bounded by systemd's start limit. This avoids
competing restart managers (see Docker's official restart-policy documentation:
https://docs.docker.com/engine/containers/start-containers-automatically/).

Deployment and supervision coordinate using the deployment lock. After updating a
release, rerun the installer with its new full SHA to update the service pin and
restart its supervisor. Do not reboot between deployment and this step. A normal
repeat of the same installed release does not restart the service. `deploy.py`
alone starts containers but is no longer a replacement for installing the boot unit.
The supervisor monitors containers after startup, not every external readiness
condition; loss of all upstreams needs the later full readiness/alerting work.

The reboot baseline includes a namespaced Redis marker with a seven-day TTL and
root-only file hashes. Verification requires a different boot ID, a current-boot
readiness record, preserved containers/data/config and observed unit ordering.
The baseline is retained for the screenshot gate; do not overwrite it to conceal
a failed boot. `verify-secrets.py` remains available after reboot.

### Stage 6: pipeline lineage and an independent client

The validator embeds a unique completed-cycle ID, completion time and checked/healthy
counts in each published snapshot; a separate `validator:last_success:v1` records the
latest cycle with a usable upstream. Reserve and active snapshots carry their actual
validator cycle and raw/validated source timestamps. Readiness rejects empty/stale
pools, a missing/stale successful cycle, recently reranked stale source data and active
entries absent from validated/reserve pools. Adjacent fresh cycles may overlap during
publication; bounded retries handle this without pretending that ranking renews validation.

On Proxmox, `SSH_PUBLIC_KEYS=/root/tg-rollout/approved-keys.pub bash
clone-client.sh` creates VM215 from clean template9001. The script uses the sibling
identity-only renderer. VM215 has eth0 management192.168.88.115/24 with no default
route and eth1 client192.168.91.2/24 with sole default via192.168.91.1. No third NIC,
application, credentials or package installation is needed. Python and SSH come from
the Debian cloud image. Protect existing VMIDs; never start another clone at these IPs.

On trusted operator ARIS, after establishing the new client's SSH host key, run:

```sh
python3 /root/TransparentGateway/deploy/verify-readiness.py \
  --gateway tgadmin@192.168.88.114 --client tgadmin@192.168.88.115
```

The operator checks the gateway over strict SSH, then sends the code-only client probe
through the client's SSH stdin; no SSH private keys are placed on either VM. The client
checks its route/source IP and opens a normal TCP socket to local echo, without explicit
proxy settings or credentials. Success requires a fresh matching request ID, the active
upstream's observed exit IP and no proxy-auth/forwarding identity headers. Thus a gateway
host-only CONNECT test cannot pass as client E2E evidence.

`LAB_READINESS PASS` covers the six checks in stage6 against the isolated local fixture.
Public HTTPS echo, client DNS and public-egress/no-bypass tests remain stages7–8. The
fixture proxy is deliberately restricted to local echo and cannot serve public DNS or
internet destinations. This result is not public/production readiness. The gateway-side
service also uses the stronger pipeline checks at startup; update its pinned settings
with install-service.py after deployment as described above.

### Public upstream and full client readiness in the isolated lab

After verifying the encrypted handoff, run `deploy/lab/enable-public-upstream.py`
as root on the lab gateway. It validates the three root-only handoff files, keeps
the local fixture credential, adds the HTTPS proxy credential with a scoped CA,
and creates a protected backup of the prior runtime. Real endpoint addresses and
CA certificates live only in ignored runtime/secret files. The optional sixth
transfer file is `secrets/upstream-ca.crt`; it is mounted read-only alongside the
credential store. Only proxy TLS connections using that credential load this CA;
public echo TLS still uses the system trust store.

Deploy the pinned commit with `--lab-fixture`, then run `install-service.py` with
the same commit. The lab override validates the explicitly matched local fixture
against the local echo; the public proxy is validated against the configured
HTTPS echo. Both must enter the active pool. Local and public targets exercise
separate upstreams; failed CONNECTs fall back to another validated candidate.
The persistent FORWARD drop rule for CLIENT_CIDR blocks unhandled client traffic,
including non-DNS UDP, while TCP and DNS are intercepted into local sockets.

From the operator host:

```sh
python3 deploy/verify-readiness.py --public \
  --gateway tgadmin@192.168.88.114 --client tgadmin@192.168.88.115
```

`FULL_CLIENT_READINESS PASS` requires fresh pool lineage, local relay echo,
a fresh UDP DNS response to the client, certificate-verified HTTPS to the public
echo at a DNS-resolved address, both echo exits present in the active pool,
request correlation and forwarding guard counters. Addresses of public exits
are hidden in the human-readable output. This is a lab route/readiness check,
not a complete anonymity audit: the public endpoint observes headers after
Cloudflare. Reboot and clean-clone reproducibility remain separate tests.

### Full reboot evidence

Before reboot, archive the earlier root-only reboot baseline and remove only its
namespaced verification marker from Redis. On the gateway run
`sudo python3 deploy/verify-boot.py --prepare` for the current service pin.
It records all runtime files, including the optional proxy CA, container IDs,
boot ID and a fresh Redis persistence marker. Reboot the test VM and do not start
systemd units or Compose manually. Wait for automatic readiness, then on ARIS:

```sh
python3 deploy/verify-full-boot.py
```

The combined check verifies a new boot, dependency ordering, preserved container
IDs/data/config/CA, current-boot readiness and fresh client DNS, local/public echo
and forwarding guard. It prints boot time and systemd readiness time. Keep the
baseline for screenshot verification; it contains protected state and must not
be shown in the article or committed.
