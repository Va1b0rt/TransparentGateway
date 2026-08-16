FROM python:3.12-slim
RUN pip install --no-cache-dir redis==5.2.1
COPY validator.py /app/validator.py
COPY connectors.py /app/connectors.py
USER 65532:65532
CMD ["python", "/app/validator.py"]
