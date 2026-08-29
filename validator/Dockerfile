FROM python:3.12-slim
RUN pip install --no-cache-dir redis==5.2.1 maxminddb==3.1.1 \
    && mkdir -p /asn-db \
    && chown 65532:65532 /asn-db
COPY validator/*.py /app/
COPY validator/sources/ /app/sources/
COPY common/ /app/common/
USER 65532:65532
CMD ["python", "/app/validator.py"]
