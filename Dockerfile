FROM python:3.13-slim

# glibc base: DuckDB publishes manylinux wheels, not musl ones
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data PORT=8080

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY beaconview ./beaconview

RUN useradd --system --uid 10001 beaconview && mkdir -p /data && chown beaconview /data
USER beaconview
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8080'), timeout=4)"

CMD ["python", "-m", "beaconview"]
