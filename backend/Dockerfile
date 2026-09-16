FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app/backend

COPY backend/ ./
RUN pip install .

COPY deploy/ /app/deploy/

RUN useradd --create-home --system --uid 10001 grouproxy \
    && mkdir -p /var/lib/grouproxy/backups \
    && chown -R grouproxy:grouproxy /app /var/lib/grouproxy

USER grouproxy

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
