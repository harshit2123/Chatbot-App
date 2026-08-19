FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The telemetry SDK is a standalone package with no dependency on this app, so
# it installs like any third-party library. Copied first: it changes far less
# often than app code, so the layer stays cached.
COPY sdk ./sdk
RUN pip install --no-cache-dir ./sdk

COPY backend/app ./app
# Migrations run at startup, so the revision scripts must ship in the image.
COPY backend/migrations ./migrations
COPY backend/alembic.ini ./alembic.ini

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
