# MarNar Trader core — arm64-compatible (python:3.12-slim is multi-arch)
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY trader ./trader
RUN pip install --no-cache-dir .

RUN useradd --create-home trader && mkdir -p /data && chown trader:trader /data
USER trader
VOLUME /data

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if b'ok' in urllib.request.urlopen('http://localhost:8000/api/health', timeout=4).read() else 1)"

CMD ["uvicorn", "trader.main:app", "--host", "0.0.0.0", "--port", "8000"]
