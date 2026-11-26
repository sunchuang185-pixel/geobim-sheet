FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8765 \
    GEOBIM_DATA_DIR=/data

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY geobim_sheet ./geobim_sheet

RUN pip install --no-cache-dir . && \
    mkdir -p /data && \
    useradd -m -u 10001 geobim && \
    chown -R geobim:geobim /app /data

USER geobim
EXPOSE 8765

CMD ["sh", "-c", "python -m geobim_sheet --host ${HOST:-0.0.0.0} --port ${PORT:-8765}"]
