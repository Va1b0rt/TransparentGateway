#!/bin/sh
set -eu

: "${CLIENT_CIDR:?CLIENT_CIDR must be an explicitly authorised subnet}"
: "${REDIS_URL:?REDIS_URL is required}"
: "${DNS_DEFAULT_RESOLVER_IPS:?DNS_DEFAULT_RESOLVER_IPS is required}"
: "${DNS_DOH_IP:?DNS_DOH_IP is required}"
: "${DNS_DOH_SERVER_NAME:?DNS_DOH_SERVER_NAME is required}"
if [ "$CLIENT_CIDR" = "0.0.0.0/0" ]; then
  echo "Refusing to intercept all IPv4 traffic; set a specific authorised CLIENT_CIDR." >&2
  exit 64
fi

cleanup() {
  iptables -t nat -D PREROUTING -s "$CLIENT_CIDR" -p tcp -j TG_TCP 2>/dev/null || true
  iptables -t nat -D PREROUTING -s "$CLIENT_CIDR" -p udp --dport 53 -j TG_DNS_DOH 2>/dev/null || true
  iptables -t nat -D OUTPUT -m mark --mark 0x53/0xff -p tcp -j REDIRECT --to-ports 12345 2>/dev/null || true
  iptables -t mangle -D PREROUTING -s "$CLIENT_CIDR" -p udp --dport 53 -j TG_DNS_TPROXY 2>/dev/null || true
  iptables -t nat -F TG_TCP 2>/dev/null || true
  iptables -t nat -F TG_DNS_DOH 2>/dev/null || true
  iptables -t nat -X TG_TCP 2>/dev/null || true
  iptables -t nat -X TG_DNS_DOH 2>/dev/null || true
  iptables -t mangle -F TG_DNS_TPROXY 2>/dev/null || true
  iptables -t mangle -X TG_DNS_TPROXY 2>/dev/null || true
  ip rule del fwmark 0x1/0xff lookup 100 priority 100 2>/dev/null || true
  ip route del local 0.0.0.0/0 dev lo table 100 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 0' INT TERM

iptables -t nat -N TG_TCP 2>/dev/null || true
iptables -t nat -N TG_DNS_DOH 2>/dev/null || true
iptables -t mangle -N TG_DNS_TPROXY 2>/dev/null || true
iptables -t nat -F TG_TCP
iptables -t nat -F TG_DNS_DOH
iptables -t mangle -F TG_DNS_TPROXY
iptables -t nat -C PREROUTING -s "$CLIENT_CIDR" -p tcp -j TG_TCP 2>/dev/null || iptables -t nat -A PREROUTING -s "$CLIENT_CIDR" -p tcp -j TG_TCP
iptables -t nat -A TG_TCP -p tcp -j REDIRECT --to-ports 12345
iptables -t nat -C PREROUTING -s "$CLIENT_CIDR" -p udp --dport 53 -j TG_DNS_DOH 2>/dev/null || iptables -t nat -A PREROUTING -s "$CLIENT_CIDR" -p udp --dport 53 -j TG_DNS_DOH
iptables -t mangle -C PREROUTING -s "$CLIENT_CIDR" -p udp --dport 53 -j TG_DNS_TPROXY 2>/dev/null || iptables -t mangle -A PREROUTING -s "$CLIENT_CIDR" -p udp --dport 53 -j TG_DNS_TPROXY
for resolver in $(printf '%s' "$DNS_DEFAULT_RESOLVER_IPS" | tr ',' ' '); do
  iptables -t nat -A TG_DNS_DOH -p udp -d "$resolver" -j REDIRECT --to-ports 5053
  iptables -t mangle -A TG_DNS_TPROXY -d "$resolver" -j RETURN
done
iptables -t mangle -A TG_DNS_TPROXY -p udp -j TPROXY --on-ip 127.0.0.1 --on-port 5353 --tproxy-mark 0x1/0x1
iptables -t nat -C OUTPUT -m mark --mark 0x53/0xff -p tcp -j REDIRECT --to-ports 12345 2>/dev/null || iptables -t nat -A OUTPUT -m mark --mark 0x53/0xff -p tcp -j REDIRECT --to-ports 12345
ip rule show | grep -Fq 'fwmark 0x1 lookup 100' || ip rule add fwmark 0x1/0xff lookup 100 priority 100
ip route replace local 0.0.0.0/0 dev lo table 100

python /app/doh_dns.py &
redsocks -c /etc/redsocks.conf &
python /app/udp2tcp_dns.py &
python /app/egress_relay.py &
wait $!
