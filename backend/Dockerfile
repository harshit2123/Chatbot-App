FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
# Migrations run at startup, so the revision scripts must ship in the image.
COPY backend/migrations ./migrations
COPY backend/alembic.ini ./alembic.ini
# The API is the task producer, so it needs the task definitions and the
# redaction module for the synchronous fallback path.
COPY worker ./worker

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
