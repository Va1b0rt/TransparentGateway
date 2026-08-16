FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    iptables iproute2 redsocks tini ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir redis==5.2.1
COPY redsocks.conf /etc/redsocks.conf
COPY udp2tcp_dns.py /app/udp2tcp_dns.py
COPY doh_dns.py /app/doh_dns.py
COPY egress_relay.py /app/egress_relay.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod 0755 /app/entrypoint.sh
ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
