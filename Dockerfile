FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=7860 \
    GEOBIM_DATA_DIR=/tmp/geobim_review \
    GEOBIM_ALLOW_UPLOAD=false \
    LLM_PROVIDER=demo

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY geobim_sheet ./geobim_sheet

RUN pip install --no-cache-dir . "gunicorn>=22,<24" && \
    mkdir -p /data /tmp/geobim_review && \
    useradd -m -u 1000 user && \
    chown -R user:user /app /data /tmp/geobim_review

USER user
EXPOSE 7860

CMD ["sh", "-c", "gunicorn --workers 1 --threads 4 --timeout 120 --bind ${HOST:-0.0.0.0}:${PORT:-7860} 'geobim_sheet.app:create_app()'"]
