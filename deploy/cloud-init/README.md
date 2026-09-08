# Stage 2: first-boot host bootstrap

The clean template stays unbooted and unchanged. For each clone, Proxmox generates
MAC-bound network/instance metadata; `render-stage2-user-data.py` generates user-data
from a clean committed checkout. It includes identity, approved **public** SSH keys,
Debian packages and a small host-bootstrap entry point. Application secrets are
delivered separately in a later stage.

Debian 13 provides Compose v2 in the `docker-compose` package, including the Docker
CLI plugin used by `docker compose`. The package name is not
`docker-compose-plugin` (which belongs to Docker's own APT repository).
See [Debian's package file list](https://packages.debian.org/trixie/amd64/docker-compose/filelist).

Cloud-init updates package lists, upgrades base packages, installs Docker/Compose,
Git, CA certificates, networking tools and QEMU guest agent. Its `runcmd` starts
`tg-bootstrap.service` after installation. Bootstrap verifies Debian 13, all three
expected IPv4 interface addresses, the sole management default route, responsive
Docker Engine, Compose v2+, guest agent and its own recorded source hash. It writes
an atomic root-owned record at `/var/lib/transparent-gateway/bootstrap.json`.

`HOST_READY` means these **host prerequisites** passed. It does not mean gateway
application readiness: no repository checkout, inventory, credentials, containers
or proxy pool are deployed in this stage. The record explicitly says
`application_deployment: pending`.

The bootstrap unit is started by cloud-init once, not enabled for every boot.
Docker and the guest agent are enabled. Runtime application ordering and full
readiness across reboots are later implementation stages. Ordering bootstrap after
`cloud-final.service` would deadlock because cloud-final starts it synchronously.

The source commit and embedded bootstrap SHA256 are recorded alongside the script.
This establishes traceability, not an independent signature/security boundary.
APT packages receive current Debian updates; their installed versions are captured
in the evidence. The stage does not claim byte-for-byte package reproducibility.

## Run on the Proxmox node

Transfer a committed checkout of this branch to the Proxmox node. Use a Git bundle
when the implementation branch has not been pushed. The renderer refuses modified
tracked source. Retain the public-key file and `tg-snippets` storage from stage 1.
Stop the previous **test** clone before reusing its network addresses.

```sh
qm shutdown 212 --timeout 60
SSH_PUBLIC_KEYS=/root/tg-rollout/approved-keys.pub ./deploy/proxmox/clone-stage2.sh
```

Defaults: template 9001, clone 213 (`tg-repro-03`), management 192.168.88.114,
client 192.168.91.1, lab 10.10.20.2. Do not start old clones 211/212 alongside it:
they use the same addresses. The original gateway VM204 is not a target.

The new clone has fresh SSH host keys. Verify them via Proxmox guest agent before
updating the reused IP's known_hosts entry; never disable host-key verification.

## Verify inside the new clone

```sh
hostname
sudo cloud-init status --long
sudo cloud-init schema --system
sudo systemctl status tg-bootstrap.service --no-pager -l
sudo journalctl -b -u tg-bootstrap.service --no-pager -o cat
sudo /usr/local/sbin/tg-bootstrap --check
sudo docker version --format 'client={{.Client.Version}} server={{.Server.Version}}'
sudo docker compose version
```

Expected: cloud-init done, no recoverable errors, unit active (exited), valid
user/network schemas, passing checks and `HOST_READY; application deployment
pending (stage 3)`. `--check` is read-only and does not replace the completion record.

The rendered snippet contains only the explicitly generated fields and approved
files, public keys and source code. Compare its hash with guest user-data and
validate its schema. Do not display full user-data or run unrestricted Docker
inspect in article screenshots after secrets delivery begins.

Reference: [cloud-init module execution](https://docs.cloud-init.io/en/latest/reference/modules.html#runcmd).
