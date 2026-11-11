# Sandbox image for Daedalus code runs:  docker build -t daedalus-sandbox sandbox/
FROM python:3.12-slim
RUN useradd --create-home --uid 10001 runner \
 && pip install --no-cache-dir numpy pandas requests \
 && mkdir -p /workspace && chown runner /workspace
WORKDIR /workspace
USER runner
CMD ["python"]
