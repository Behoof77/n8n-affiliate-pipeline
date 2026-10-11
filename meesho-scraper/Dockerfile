# Pin bookworm: playwright 1.49.1 --with-deps references ttf-unifont /
# ttf-ubuntu-font-family, which were removed in Debian trixie.
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /srv

# Chromium + OS deps (playwright's --with-deps installs the shared libraries)
RUN pip install --no-cache-dir -r /dev/stdin <<'EOF'
playwright==1.49.1
EOF
RUN playwright install --with-deps chromium

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd -m runner && chown -R runner:runner /srv
USER runner

EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:5000/health', timeout=4)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5000"]
