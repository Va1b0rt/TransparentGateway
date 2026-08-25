# Transparent Gateway

Transparent Gateway routes authorised client traffic through administrator-supplied
HTTP, HTTPS, SOCKS4, or SOCKS5 upstream proxies. It provides transparent TCP
proxying and two DNS paths while preserving the destination selected by the client.

The project is intended for networks and proxy services you administer or are
explicitly authorised to use. It does not collect public proxies or implement
CAPTCHA, blocking, or rate-limit evasion.

## Related articles

- [Прозоре проксіювання TCP: redsocks + HAProxy](https://devdays.net.ua/articles/prozore-proksiiuvannia-tcp-redsocks-haproxy/)
- [Витік DNS, або будуємо велосипед](https://devdays.net.ua/articles/vitik-dns-abo-buduiemo-velosiped/)

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
  each proxy, rejects client-IP leaks, and records end-to-end latency plus the
  consecutive-success count.
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

Fetcher, validator, and ranker run as separate unprivileged Compose services.
They exchange complete JSON snapshots through Redis instead of calling each
other directly. Every publication writes a unique temporary key and atomically
renames it over the previous snapshot, so readers never observe a partial pool.

The connector config provides two explicit lists: connector types permitted for
this deployment and connector instances that are active. An active connector must
also exist in the in-code registry and in `allowed_connector_types`.

The ranker combines measured latency and a bounded consecutive-success bonus. If
an authorised source supplies `metadata.asn`, repeated ASN values are penalised;
repeated IPv4 `/24` or IPv6 `/48` networks are also penalised. A pool manager may
consume the reserve key, while the included egress relay consumes the compatibility
active key.

### Protocol and anonymity validation

The validator does more than open the candidate's TCP port. It performs the real
proxy negotiation and requests an operator-controlled echo endpoint through the
candidate:

- HTTP and TLS-wrapped HTTP proxy requests;
- SOCKS4/SOCKS4a `CONNECT`;
- SOCKS5 no-auth negotiation and `CONNECT`, with proxy-side hostname resolution;
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

- Linux with IPv4 TPROXY, policy routing, `iptables` and connection marks;
- Docker Engine and Docker Compose;
- root access on the gateway host;
- a route from the client subnet to the gateway;
- one or more authorised HTTP/HTTPS/SOCKS upstream proxies;
- destination resolvers that support DNS over TCP for the explicit-resolver path.

This implementation is Linux- and IPv4-specific. Constants such as
`IP_TRANSPARENT`, `IP_RECVORIGDSTADDR`, `IP_PKTINFO`, and `SO_MARK` use the Linux
socket API.

## Configuration

Create the runtime files from the supplied examples:

```sh
cp .env.example .env
cp inventory/connectors.json.example inventory/connectors.json
cp inventory/upstreams.jsonl.example inventory/upstreams.jsonl
cp secrets/proxy-credentials.json.example secrets/proxy-credentials.json
```

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
| `RANK_INTERVAL_SECONDS` | Interval between reserve/active pool publications. |

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
`POOL_SIZE` entries into the active snapshot. Keep `POOL_SIZE` less than or equal
to `RESERVE_SIZE` so a reserve remains available after a failed endpoint enters
cooldown. Set `MIN_SCORE_THRESHOLD` when quality should determine the final count;
the active pool may then contain fewer than `POOL_SIZE` entries.

Choose these values from observed connection concurrency, upstream capacity, and
the desired failure headroom rather than copying `150`. For a small lab, a pool
of 8–20 endpoints is usually enough; larger authorised fleets may benefit from a
larger reserve. The current implementation ranks authorised candidates by
protocol-level reachability, anonymity, latency, stability, and supplied network
metadata. It does not perform public-proxy discovery.

### Connector registry

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
`credential_ref`, and optional string metadata. The legacy `endpoint` form remains
accepted at the input boundary; snapshots contain both forms for gateway
compatibility. An unauthenticated SOCKS5 candidate can be as small as:

```json
{"host":"proxy.example","port":1080,"protocol":"socks5"}
```

Supported values for `protocol` are `http`, `https`, `socks4`, and `socks5`.
Proxy authentication is optional. When a gateway connection needs credentials,
the inventory carries only an opaque reference and the gateway resolves it from
the ignored secret file:

```json
{
  "provider-1": {
    "username": "example-user",
    "password": "example-password"
  }
}
```

Keep `.env`, the real inventory, and `secrets/proxy-credentials.json` out of version
control. Restrict the credentials file to its owner:

```sh
chmod 600 secrets/proxy-credentials.json
```

## Network setup

For an isolated test, use a dedicated VLAN or subnet and keep management traffic on
a separate network. One possible layout is:

```text
Management network: 192.168.88.0/24
Gateway management:  192.168.88.104

Test VLAN 90:         192.168.90.0/24
Gateway VLAN address: 192.168.90.1
Test client:          192.168.90.10
Client default route: 192.168.90.1
```

For this layout:

```env
CLIENT_CIDR=192.168.90.0/24
DNS_DEFAULT_RESOLVER_IPS=1.1.1.1,1.0.0.1
DNS_DOH_IP=1.1.1.1
DNS_DOH_SERVER_NAME=cloudflare-dns.com
```

The switch or virtual bridge must carry the selected VLAN tag between the gateway
and client. Do not move the management default route into the intercepted subnet.

## Start

Review `gateway/entrypoint.sh` before deployment because it modifies host
`iptables` and policy-routing state. Then start the stack:

```sh
docker compose up --build -d
docker compose ps
docker compose logs -f validator gateway
```

Inspect the current validated pool without displaying credentials:

```sh
docker compose exec redis \
  redis-cli GET transparent-gateway:validated:v1
```

The container healthcheck confirms that the local egress relay accepts TCP
connections. It does not replace end-to-end DNS tests.

## Verify DNS routing

Run ordinary and explicit-resolver queries on a client inside `CLIENT_CIDR`:

```sh
dig example.com +short
dig @8.8.8.8 example.com +short
```

On the gateway, check which branches handled the packets:

```sh
iptables -t nat -vnL TG_DNS_DOH
iptables -t mangle -vnL TG_DNS_TPROXY
```

The first query increments `TG_DNS_DOH`; the second increments the TPROXY rule in
`TG_DNS_TPROXY`.

To prove that neither branch connects directly to its resolver, capture the proxy
endpoint and both possible direct destinations on the external interface:

```sh
tcpdump -nn -ni eth0 \
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
iptables-save
ip rule show
ip route show table 100
```

## Tests

Run the unit tests from the repository root:

```sh
python -m pip install -r requirements-dev.txt
python -m pytest tests
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
- authenticated-only proxies need a credential-aware validation policy; the
  default validator deliberately offers SOCKS5 no-auth and sends no proxy auth;
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
