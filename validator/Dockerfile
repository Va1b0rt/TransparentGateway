FROM python:3.12-slim
RUN pip install --no-cache-dir redis==5.2.1
COPY *.py /app/
USER 65532:65532
CMD ["python", "/app/validator.py"]
