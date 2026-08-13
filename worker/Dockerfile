FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

# The worker reuses the backend's models and schemas so the DB write and the
# validation contract cannot drift between producer and consumer.
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY worker ./worker

CMD ["celery", "-A", "worker.celery_app.celery_app", "worker", "--loglevel=info", "--concurrency=2"]
