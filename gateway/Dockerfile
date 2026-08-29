FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    iptables iproute2 redsocks tini ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir redis==5.2.1
COPY gateway/redsocks.conf /etc/redsocks.conf
COPY gateway/udp2tcp_dns.py /app/udp2tcp_dns.py
COPY gateway/doh_dns.py /app/doh_dns.py
COPY gateway/egress_relay.py /app/egress_relay.py
COPY gateway/entrypoint.sh /app/entrypoint.sh
COPY common/ /app/common/
RUN chmod 0755 /app/entrypoint.sh
ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
