FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tournament_setup.py scheduler.py event_example.toml ./
COPY webapp ./webapp

RUN useradd --create-home --uid 1000 planner && mkdir -p /data && chown planner:planner /data
USER planner
VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"
CMD ["uvicorn", "webapp.server:app", "--host", "0.0.0.0", "--port", "8000"]
