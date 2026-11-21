# The scorer builds this on linux/amd64 and runs it with no network. The
# digest is the image we labeled every commit in: its Python and SQLite
# decide which sqlglot tests run, so keep it if your image runs them.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
COPY blast-radius-sample/selector.py blast-radius-sample/impact-map.json blast-radius-sample/optimizer-fixtures.json blast-radius-sample/test-dependencies.json blast-radius-sample/enum-domains.json blast-radius-sample/verification.py blast-radius-sample/probe_runner.py blast-radius-sample/probe-snapshot.b85 blast-radius-sample/class_dependencies.py blast-radius-sample/class-domains.json /app/
RUN python -B -S /app/verification.py
CMD ["python", "-B", "-S", "/app/selector.py"]
