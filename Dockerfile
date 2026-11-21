# Generate map.json.gz locally using `make map`, then commit it as a plain file.
# Official builds only copy the baseline map, avoiding repeated test runs.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1
COPY map.json.gz selector.py impact.py trace_map.py /app/
CMD ["python", "-B", "/app/selector.py"]
