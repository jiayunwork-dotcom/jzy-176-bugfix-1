FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DECK_DB_PATH=/data/decks.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 单容器够用；多 worker 时应把 SQLite 放到共享卷并开启 WAL（已在代码中开启）。
CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
