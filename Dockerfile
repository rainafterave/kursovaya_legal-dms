FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 legal \
    && useradd --uid 10001 --gid legal --no-create-home legal \
    && mkdir -p /data/files \
    && chown legal:legal /data/files
COPY --chown=legal:legal app/ .
USER legal
EXPOSE 8000
CMD ["sh", "-c", "python app.py init-db && exec gunicorn --bind 0.0.0.0:8000 --workers 2 --threads 2 --timeout 60 --access-logfile - --error-logfile - app:app"]
