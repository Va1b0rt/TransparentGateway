# Transparent Gateway

English | [Українська](README.uk.md)

Transparent Gateway routes authorised client traffic through administrator-supplied
HTTP, HTTPS, SOCKS4, or SOCKS5 upstream proxies. It provides transparent TCP
proxying and two DNS paths while preserving the destination selected by the client.

The project is intended for networks and proxy services you administer or are
explicitly authorised to use. It does not collect public proxies or implement
CAPTCHA, blocking, or rate-limit evasion.

For a clean installation, start with [Requirements](#requirements), then follow
[Network layout for the first run](#network-layout-for-the-first-run) and
[First run](#first-run). The architecture and configuration sections below explain
what those commands create.

## Related articles

- [Прозоре проксіювання TCP: redsocks + HAProxy](https://devdays.net.ua/articles/prozore-proksiiuvannia-tcp-redsocks-haproxy/)
- [Витік DNS, або будуємо велосипед](https://devdays.net.ua/articles/vitik-dns-abo-buduiemo-velosiped/)
- [Від GitHub до робочого Transparent Gateway](https://devdays.net.ua/articles/transparent-gateway-pislia-git-clone/) — first installation on Debian, proxy configuration, systemd startup, and a client DNS/HTTPS check (Ukrainian).

## How it works

```text
Authorised client subnet (CLIENT_CIDR)
│
├─ TCP
│  └─ iptables REDIRECT :12345
│     └─ redsocks → egress relay :12000 → upstream proxy → destination
│
└─ UDP/53
   ├─ destination in DNS_DEFAULT_RESOLVER_IPS
   │  └─ REDIRECT :5053 → DNS-over-HTTPS
   │     └─ marked TCP → redsocks → upstream proxy → DNS_DOH_IP:443
   │
   └─ any other resolver address
      └─ TPROXY :5353 → UDP-to-TCP DNS relay
         └─ marked TCP → redsocks → upstream proxy → original resolver:53
```

Both DNS branches use the configured upstream proxy pool. They differ only in how
the DNS message reaches its resolver:

- ordinary system lookups use DNS-over-HTTPS to the configured public resolver;
- a query addressed to another resolver is converted from UDP DNS to DNS-over-TCP
  and delivered to that original address.

The gateway cannot see whether an application used syntax such as `dig name` or
`dig @server name`; it makes the decision from the destination IP of the packet.

## Components

- **gateway** is the only privileged container. It runs `iptables`, `redsocks`,
  the egress relay, the DoH relay, and the UDP-to-TCP DNS relay in the host network
  namespace.
- **fetcher** loads candidates concurrently from explicitly active, authorised
  inventory and HTTPS feed connectors.
- **validator** performs bounded asynchronous HTTP(S)/SOCKS4/SOCKS5 checks through
  each proxy (including configured credentials), rejects client-IP leaks, enriches
  the observed exit IP from a local ASN database, and records end-to-end latency
  plus the consecutive-success count.
- **asn-updater** downloads the PDDL-licensed origin ASN MMDB, verifies its
  published SHA-256 checksum, and atomically refreshes the shared local database.
- **ranker** applies latency/stability scoring, optional ASN/subnet diversity
  penalties, and publishes configurable reserve and active pools.
- **redis** stores the latest validated pool with AOF persistence.

### Authorised proxy pipeline

The pool pipeline replaces manual active-pool maintenance while keeping source
admission explicit. It does not scrape arbitrary public proxy lists. Each source
implements `ProxySourceConnector` from `validator/sources/base.py`. Concrete
connectors live in `validator/sources/` and are registered manually in
`validator/sources/__init__.py`; merely placing a Python file in that directory
does not execute or enable it.

```text
configured connectors
       │
       ▼
fetcher  ──→ proxy_pool:raw:v1
                    │
                    ▼
validator ─→ proxy_pool:validated:v1
                    │
                    ▼
ranker   ──→ proxy_pool:reserve:v1
                    │
                    ├──→ proxy_pool:active:v1
                    └──→ transparent-gateway:validated:v1 (gateway compatibility)
```

Fetcher, ASN updater, validator, and ranker run as separate unprivileged Compose services.
They exchange complete JSON snapshots through Redis instead of calling each
other directly. Every publication writes a unique temporary key and atomically
renames it over the previous snapshot, so readers never observe a partial pool.

The connector config provides two explicit lists: connector types permitted for
this deployment and connector instances that are active. An active connector must
also exist in the in-code registry and in `allowed_connector_types`.

The ranker combines measured latency and a bounded consecutive-success bonus.
Repeated verified exit ASN values are penalised; repeated IPv4 `/24` or IPv6
`/48` networks are also penalised. A pool manager may
consume the reserve key, while the included egress relay consumes the compatibility
active key.

### Protocol and anonymity validation

The validator does more than open the candidate's TCP port. It performs the real
proxy negotiation and requests an operator-controlled echo endpoint through the
candidate:

- HTTP and TLS-wrapped HTTP proxy requests;
- SOCKS4/SOCKS4a `CONNECT`;
- SOCKS5 no-auth or username/password negotiation and `CONNECT`, with proxy-side
  hostname resolution;
- an HTTP or HTTPS request through the established route.

The validator first requests the same echo endpoint directly to learn its own
observed identity. A candidate is rejected when that identity is still the
endpoint peer, or appears in `Forwarded`, `X-Forwarded-For`, `X-Real-IP`,
`Client-IP`, or another known forwarding header. A proxy that hides the identity
but sends `Via` or another proxy marker is accepted as `anonymous`; one without
such markers is recorded as `elite`. `Via` reveals proxy use, not the client's IP,
so it is not by itself an anonymity failure.

The echo service must return JSON with `backend_peer` (or `origin`) and a `headers`
object. `test-echo.py` supplies this contract for the isolated lab. Keep this
endpoint under your control: a third-party checker sees every candidate exit IP
and becomes part of the validation trust boundary.

ASN enrichment uses the exit address observed by that endpoint, not an ASN claimed
by a source connector. Source-provided ASN is retained only as `source_asn` for
diagnostics. The ranker reads verified `network.asn`; unknown values are penalised
and capped in the active pool. If the local MMDB is missing, invalid, or stale, the
validator leaves the previous complete snapshot in place.

When a candidate has `credential_ref`, both the validator and egress relay resolve
it from the same read-only credential store. HTTP(S) supports Basic proxy auth,
SOCKS5 supports username/password negotiation, and SOCKS4 uses the username as its
User ID. Secrets are never written to Redis or logs.

### DNS relays

`gateway/doh_dns.py` accepts a raw UDP DNS message, wraps it in an HTTPS POST using
the `application/dns-message` media type, and validates the TLS certificate with
the configured SNI name. The DoH endpoint is addressed by a fixed IP, avoiding a
bootstrap DNS dependency. Its TCP socket is marked `0x53`, so the HTTPS connection
also passes through the upstream proxy.

`gateway/udp2tcp_dns.py` receives non-default DNS destinations with TPROXY, reads
the original resolver address, adds the two-byte DNS-over-TCP length prefix, and
performs the exchange through a marked TCP socket. It returns the result through
an `IP_TRANSPARENT` UDP socket bound to the original resolver IP and port 53, so
the client sees a response from the server it queried.

For port 53, the egress relay verifies DNS-over-TCP through a disposable tunnel.
Failed candidates enter a cooldown; successful candidates are preferred for later
DNS requests. The real client request always receives a fresh tunnel.

## Requirements

- a Linux host with systemd, IPv4 TPROXY, policy routing, `iptables`, and
  connection marks (the documented first run is tested on Debian 13);
- Git and Python 3;
- Docker Engine with the Docker Compose plugin;
- root access on the gateway host;
- a dedicated client subnet routed through the gateway;
- one or more authorised HTTP/HTTPS/SOCKS upstream proxies;
- destination resolvers that support DNS over TCP for the explicit-resolver path;
- a separate Linux client with Python 3 for the end-to-end check.

This implementation is Linux- and IPv4-specific. Constants such as
`IP_TRANSPARENT`, `IP_RECVORIGDSTADDR`, `IP_PKTINFO`, and `SO_MARK` use the Linux
socket API.

Verify the required commands before cloning the repository:

```sh
git --version
python3 --version
docker --version
docker compose version
systemctl --version
```

The project does not install Docker or configure host network interfaces. Use the
Docker packages appropriate for your distribution and make sure Docker is running:

```sh
sudo systemctl enable --now docker
```

## Network layout for the first run

The gateway needs an address in the client subnet. The client must use that
address as its only IPv4 default gateway. A separate management path is strongly
recommended so a routing mistake does not lock you out of either machine.

This guide uses one example consistently:

```text
Client network:         192.168.91.0/24
Gateway client address: 192.168.91.1
Test client address:    192.168.91.2
Client default route:   192.168.91.1
```

`CLIENT_CIDR` is the whole client network (`192.168.91.0/24`), not the gateway's
individual address. Assigning `192.168.91.1` to the gateway interface and setting
the client's default route are host or hypervisor tasks; `tgctl.py` deliberately
does not change them.

## First run

Clone the repository on the Linux host that will route the client subnet. The
interactive configurator asks for that subnet and one authorised proxy. A proxy
password is read without echoing it and is written only to the ignored credential
file.

```sh
git clone https://github.com/Va1b0rt/TransparentGateway.git
cd TransparentGateway
sudo python3 scripts/tgctl.py configure
```

The prompts mean:

| Prompt | What to enter |
| --- | --- |
| Client IPv4 subnet | The network behind this gateway, not its management network. In the example topology below: `192.168.91.0/24`. |
| Proxy protocol | The protocol stated by the proxy operator: `http`, `https`, `socks4`, or `socks5`. This is not the protocol of the destination website. |
| Proxy host or IP | The authorised upstream proxy address supplied by its operator. Do not enter the gateway or echo address. |
| Proxy port | The port supplied together with that proxy address. |
| Username and password | Answer yes only when the upstream proxy requires them, then enter the credentials supplied by its operator. |
| Validation echo URL | Press Enter for the public first-run endpoint, or enter the URL of your own compatible echo service. |

The proxy address and credentials do not come from this repository. They belong to
the proxy service you administer or are authorised to use.

`configure` creates four runtime files with restricted ownership and permissions:

```text
.env
inventory/connectors.json
inventory/upstreams.jsonl
secrets/proxy-credentials.json
```

They are ignored by Git. The command refuses to overwrite them; edit the existing
files when changing a running installation instead of deleting them and entering
all values again.

Validate the generated files before the privileged gateway container is started:

```sh
sudo python3 scripts/tgctl.py check
```

The check validates file ownership and permissions, the canonical `host`/`port`
inventory format, credential references, the client subnet, the echo URL, and the
rendered Compose configuration. It does not change `iptables` or routing rules.

Start the six services and wait until the supplied proxy reaches the active pool:

```sh
sudo python3 scripts/tgctl.py up --timeout 300
```

The first `up` builds the images, starts the containers, waits for a usable proxy,
and installs `/etc/systemd/system/transparent-gateway.service`. The service uses
this checkout on later boots, so do not move or delete the repository directory.
The Compose services intentionally use `restart: "no"`; systemd owns their
lifecycle.

Use systemd after the first successful start:

```sh
sudo systemctl status transparent-gateway.service --no-pager
sudo systemctl restart transparent-gateway.service
sudo python3 scripts/tgctl.py down
sudo journalctl -u transparent-gateway.service --no-pager -n 100
```

`tgctl.py down` stops the systemd service and waits until its containers have
stopped. If the first `up` failed before installing the service, the command stops
the partially started Compose project directly. Do not use `docker compose down`:
systemd may still report the oneshot service as active after its containers are
removed behind its back.

Show a redacted service and pool summary without dumping proxy addresses or
credentials:

```sh
sudo python3 scripts/tgctl.py status
```

`ACTIVE_POOL ready=1` proves that the supplied proxy passed validation. It does not
yet prove that traffic from another machine crosses the gateway.

### Update an installed gateway

From the existing checkout, select a branch, tag, or commit available on `origin`:

```sh
sudo python3 scripts/tgctl.py update main --timeout 300
```

The updater checks for changes to tracked files, validates the candidate against
your existing configuration in a temporary worktree, and builds images while
the current containers keep running. It then replaces the containers, retaining
the Compose project and named volumes. `.env`, inventory, and credential files
are not rewritten. Changed tracked files stop the update and are listed by name.

Success requires six running/healthy services, fresh pool snapshots with a
successful validator cycle after replacement, and a real echo request through
the relay matching an active exit. `--timeout` is the readiness budget for each
attempt; recovery gets its own budget. Fetch/build have separate time limits.
Connections may be interrupted during replacement. This host check does not
replace the client route/DNS test below.

If replacement or readiness fails, the command restores the previous commit and
retained images, checks readiness again, and exits unsuccessfully even when
recovery succeeds. `ROLLBACK PASS` means the previous version works again, not
that the update succeeded. A failed recovery is reported separately. After an
interrupted update, use the preserved journal to retry recovery:

```sh
sudo python3 scripts/tgctl.py update --recover --timeout 300
```

Recovery after a killed process or host interruption is explicit. The checkout
stays at the installed commit (detached HEAD); local branches are not moved.
Later updates fetch the selected revision from `origin`. On older installations,
first obtain the release containing this command with Git.

Keep `runtime/update/` (private and ignored by Git). Its `images.json` pins images
for `tgctl.py` and systemd on later boots; `transaction.json` supports recovery.
Use `tgctl.py` or systemd rather than direct Compose commands, which omit these
image pins. Do not delete retained image tags during an update or recovery.

This updater requires the existing six services and installed systemd unit. It
refuses changes to Redis configuration or volume definitions. It does not undo
data changes inside volumes: stored-format migrations need a separate procedure.

Run the host readiness gate independently with:

```sh
sudo python3 scripts/tgctl.py ready --timeout 300
```

### Verify a real client request

Run the final check on a separate client whose only IPv4 default route points to
the gateway. Copy `scripts/client-check.py` from any trusted machine that has this
checkout and can reach the client's management address:

```sh
scp scripts/client-check.py CLIENT_USER@CLIENT_MANAGEMENT_IP:/tmp/tg-client-check.py
ssh CLIENT_USER@CLIENT_MANAGEMENT_IP
```

The management address is used only to open the terminal. On the client, verify
the route and run the check:

```sh
ip -4 route show default
ip -4 route get 1.1.1.1
python3 /tmp/tg-client-check.py --gateway 192.168.91.1
```

Both route commands must select `192.168.91.1`. A successful check looks like:

```text
CLIENT_ROUTE PASS gateway=192.168.91.1 dev=eth1
CLIENT_DNS PASS addresses=2
CLIENT_HTTPS PASS request_id=0123456789abcdef0123456789abcdef
PUBLIC_EXIT id=0123456789ab
```

- `gateway` is the next hop and `dev` is the client's selected network interface;
- `addresses` is the number of IPv4 addresses returned for the public echo host;
- `request_id` is a new random marker echoed by the service, proving that the JSON
  response belongs to this request;
- `PUBLIC_EXIT id` is a short fingerprint of the source address observed by the
  echo service.

Run `sudo python3 scripts/tgctl.py status` on the gateway and compare
`PUBLIC_EXIT id` with `active_exit_ids`. The same fingerprint on both machines
means that the client request left through a proxy in the active pool. The
fingerprint avoids printing the raw address during a routine check; it is not a
security boundary and should not be published when the address itself is secret.

The default `https://echo.devdays.net.ua/` endpoint is convenient for this first
run, but its operator can see proxy exit addresses. For continuous validation,
deploy an instance of
[TransparentGatewayEcho](https://github.com/Va1b0rt/TransparentGatewayEcho) that
you control. Provide its URL during `configure` and pass the same URL to
`client-check.py --url`.

### Verify restart recovery

After the client check passes, an optional reboot confirms that systemd can return
the gateway to service without a manual Compose command:

```sh
sudo systemctl reboot
```

Reconnect to the gateway, then run:

```sh
sudo systemctl status transparent-gateway.service --no-pager
cd /path/to/TransparentGateway
sudo python3 scripts/tgctl.py status
```

Repeat the client check after `ACTIVE_POOL` becomes ready.

## Runtime configuration

The first-run configurator is the supported way to create runtime files. Files
ending in `.example` document the format only: they contain placeholder hosts and
credentials and cannot be started unchanged. Do not copy them over files created
by `configure`.

After editing runtime files, validate and restart through the same control path:

```sh
sudo python3 scripts/tgctl.py check
sudo systemctl restart transparent-gateway.service
```

The quickstart controller accepts one local `jsonl_inventory` connector, which may
contain multiple authorised proxies. HTTP feed connectors and version-pinned
operator deployments are documented in [`deploy/README.md`](deploy/README.md);
do not enable them by bypassing `tgctl.py check` with an unreviewed Compose command.

Important environment variables:

| Variable | Purpose |
| --- | --- |
| `CLIENT_CIDR` | Only packets from this authorised IPv4 subnet are intercepted. `0.0.0.0/0` is rejected. |
| `DNS_DEFAULT_RESOLVER_IPS` | Comma-separated destinations assigned to the DoH branch. |
| `DNS_DOH_IP` | Fixed IPv4 address of the DoH server. |
| `DNS_DOH_SERVER_NAME` | TLS SNI and HTTP Host value used for certificate validation. |
| `DNS_TCP_TIMEOUT_SECONDS` | Timeout for DNS-over-TCP exchanges. |
| `DNS_MAX_CONCURRENCY` | Maximum concurrent DNS requests. |
| `POOL_SIZE` | Maximum number of validated upstreams used by the gateway. |
| `RESERVE_SIZE` | Maximum number of candidates written to the Redis snapshot. |
| `MIN_SCORE_THRESHOLD` | Optional lower score boundary applied by the ranker. |
| `FETCH_INTERVAL_SECONDS` | Interval between authorised-source refreshes. |
| `VALIDATION_INTERVAL_SECONDS` | Interval between protocol/anonymity validation cycles. |
| `ANONYMITY_CHECK_URL` | Operator-controlled JSON echo endpoint used directly and through every candidate. |
| `ANONYMITY_CLIENT_IPS` | Optional comma-separated additional direct/NAT IPs that must never be leaked. |
| `PROXY_VALIDATION_TIMEOUT_SECONDS` | End-to-end timeout for the direct check and each proxy check. |
| `ASN_MAX_DATABASE_AGE_SECONDS` | Maximum allowed age of the local verified ASN MMDB. |
| `ASN_UPDATE_INTERVAL_SECONDS` | Interval between checksum-verified ASN database refreshes. |
| `RANK_INTERVAL_SECONDS` | Interval between reserve/active pool publications. |
| `UNKNOWN_ASN_PENALTY` | Score divisor applied when an exit IP has no verified ASN. |
| `MAX_UNKNOWN_ASN_RATIO` | Maximum fraction of unknown-ASN candidates admitted to the active pool. |

### Pool sizing

`150` is an example deployment value, not a protocol limit or a constant embedded
in the code. Set `POOL_SIZE` to the number of validated upstreams the gateway may
select concurrently; the default example is `150`:

```env
POOL_SIZE=150
RESERVE_SIZE=700
MIN_SCORE_THRESHOLD=0
```

The ranker publishes at most `RESERVE_SIZE` scored candidates and copies the first
eligible `POOL_SIZE` entries into the active snapshot. Keep `POOL_SIZE` less than or equal
to `RESERVE_SIZE` so a reserve remains available after a failed endpoint enters
cooldown. Set `MIN_SCORE_THRESHOLD` when quality should determine the final count;
the active pool may then contain fewer than `POOL_SIZE` entries.

Choose these values from observed connection concurrency, upstream capacity, and
the desired failure headroom rather than copying `150`. For a small lab, a pool
of 8–20 endpoints is usually enough; larger authorised fleets may benefit from a
larger reserve. The current implementation ranks authorised candidates by
protocol-level reachability, anonymity, latency, stability, and network metadata
derived from the observed exit IP. It does not perform public-proxy discovery.

### Connector registry

This section describes the extensible connector API, not the restricted
single-connector quickstart accepted by `tgctl.py check`. Use the version-pinned
deployment workflow when enabling an HTTP feed or another operator-managed source.

The connector configuration contains only the deployment allow-list and active
instances:

```json
{
  "allowed_connector_types": ["jsonl_inventory", "http_json_feed"],
  "active_connectors": [
    {
      "type": "jsonl_inventory",
      "name": "managed-socks-pool",
      "path": "/inventory/upstreams.jsonl"
    }
  ]
}
```

To add a built-in connector, implement `collect()` as an async iterator in a new
module under `validator/sources/`, import its class, and explicitly call
`CONNECTOR_REGISTRY.register()` in `validator/sources/__init__.py`. Then allow its
type and add an active instance in `inventory/connectors.json`. Fetcher and all
downstream workers remain unchanged.

### Upstream inventory

The canonical candidate has `host`, `port`, `protocol`, `source`, optional
`credential_ref`, and optional string metadata. The core normaliser still accepts
the legacy `endpoint` form at selected connector boundaries, but the quickstart
check rejects it so new installations use separate `host` and `port` fields.
Snapshots contain both forms for gateway compatibility. An unauthenticated SOCKS5
candidate can be as small as:

```json
{"host":"proxy.example","port":1080,"protocol":"socks5"}
```

Supported values for `protocol` are `http`, `https`, `socks4`, and `socks5`.
Proxy authentication is optional. When validation or a gateway connection needs
credentials, the inventory carries only an opaque reference and both services
resolve it from the ignored secret file:

```json
{
  "provider-1": {
    "username": "example-user",
    "password": "example-password"
  }
}
```

Keep `.env`, the real inventory, and `secrets/proxy-credentials.json` out of version
control. Allow the container's unprivileged group to read the credentials without
making the file world-readable (the Compose services run with GID `65532`):

```sh
chown root:65532 secrets/proxy-credentials.json
chmod 640 secrets/proxy-credentials.json
```

## Inspect a running gateway

`tgctl.py status` is the normal overview. For a failed start, inspect the service
and recent container logs without dumping Redis snapshots or expanded Compose
configuration:

```sh
sudo systemctl status transparent-gateway.service --no-pager -l
sudo journalctl -u transparent-gateway.service --no-pager -n 100
sudo docker compose ps
sudo docker compose logs --tail 100 validator gateway
```

Redis snapshots contain proxy endpoints, observed exit addresses, and credential
references. They do not contain passwords, but they still should not be pasted
into public issues or screenshots. The container healthcheck confirms only that
the local relay accepts TCP connections; use the separate client check for the
complete route.

Review `gateway/entrypoint.sh` before using the project on a shared host because
the privileged gateway container modifies host `iptables` and policy-routing
state.

## Advanced DNS verification

Run ordinary and explicit-resolver queries on a client inside `CLIENT_CIDR`:

```sh
dig example.com +short
dig @8.8.8.8 example.com +short
```

On the gateway, check which branches handled the packets:

```sh
sudo iptables -t nat -vnL TG_DNS_DOH
sudo iptables -t mangle -vnL TG_DNS_TPROXY
```

The first query increments `TG_DNS_DOH`; the second increments the TPROXY rule in
`TG_DNS_TPROXY`.

To prove that neither branch connects directly to its resolver, capture the proxy
endpoint and both possible direct destinations on the external interface:

```sh
sudo tcpdump -nn -ni EXTERNAL_INTERFACE \
  '(host PROXY_IP and tcp port PROXY_PORT) or \
   (host 1.1.1.1 and tcp port 443) or \
   (host 8.8.8.8 and tcp port 53)'
```

During the two `dig` commands, the capture should show TCP sessions to
`PROXY_IP:PROXY_PORT` and no direct TCP sessions to `1.1.1.1:443` or
`8.8.8.8:53`.

Useful relay logs for the explicit path look like:

```text
[udp2tcp-dns] tcp response ... bytes from ('8.8.8.8', 53)
[udp2tcp-dns] udp reply sent to ('CLIENT_IP', CLIENT_PORT)
```

## iptables and policy routing

The entrypoint creates project-owned `TG_*` chains. In summary:

- client TCP is redirected to `redsocks` on port `12345`;
- UDP/53 to a default resolver is redirected to the DoH relay on port `5053`;
- other UDP/53 traffic is delivered to the transparent DNS relay on port `5353`;
- locally created TCP sockets marked `0x53` are redirected to `redsocks`;
- mark `0x1/0xff` and routing table 100 deliver TPROXY packets locally.

The full-byte policy mask is significant: `0x1/0xff` must not also match the DNS
relay mark `0x53`.

On graceful shutdown, the entrypoint removes its jumps, chains, policy rule, and
table-100 route. After a forced termination or host failure, inspect host rules
before restarting:

```sh
sudo iptables-save
sudo ip rule show
sudo ip route show table 100
```

## Tests

Run the unit tests from the repository root:

```sh
python3 -m pip install -r requirements-dev.txt
python3 -m pytest tests
```

Unit tests cover connector normalisation, secret-reference authentication, CONNECT
parsing, SOCKS5 protocol validation, anonymity classification, and
original-destination extraction. For kernel TPROXY and transparent source-address
behaviour, use the end-to-end VLAN tests above.

## Limitations

- IPv4 only;
- transparent interception covers TCP and UDP/53, not arbitrary UDP;
- QUIC/HTTP3 over UDP/443 is not proxied and should be disabled during TCP tests;
- explicit resolvers must accept DNS over TCP on port 53;
- upstream availability can change, so production use requires monitoring and a
  sufficiently reliable authorised proxy pool;
- authenticated proxies are supported with HTTP Basic, SOCKS5 username/password,
  and the SOCKS4 User ID; Digest, NTLM, Negotiate, and other schemes are not
  implemented;
- anonymity validation depends on the correctness and availability of the
  configured operator-controlled echo endpoint;
- DNS/TCP capability is confirmed separately by the gateway when a proxy is used.

## Security

- Use only proxy endpoints you own or are authorised to use.
- Keep credentials outside inventory and Redis.
- Do not expose Redis beyond loopback.
- Keep the management network outside `CLIENT_CIDR`.
- Review packet-filter rules before using the gateway on a shared host.

## License

Transparent Gateway is available under the MIT License. See `LICENSE`.
