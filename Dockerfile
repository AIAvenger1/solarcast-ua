# SolarCast UA: FastAPI backend + static frontend in one small image.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

# run as an unprivileged user
RUN useradd --system --uid 10001 solarcast
USER solarcast

# Cloud Run injects PORT; 8080 is its default and ours
ENV PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
