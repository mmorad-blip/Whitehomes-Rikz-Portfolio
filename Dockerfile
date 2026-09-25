FROM python:3.11-slim

# WeasyPrint (PDF) needs Pango; DejaVu gives it a complete font.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 rikz \
 && mkdir -p /data && chown rikz /data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
RUN pip install --no-cache-dir --no-deps .

ENV RIKZ_CONFIG_DIR=/app/config \
    RIKZ_DATA_DIR=/data \
    PYTHONUNBUFFERED=1
USER rikz
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"
CMD ["rikz", "serve", "--host", "0.0.0.0", "--port", "8000"]
