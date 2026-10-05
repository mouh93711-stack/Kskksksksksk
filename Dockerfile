FROM python:3.11-slim

WORKDIR /app

COPY . .

RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir aiogram telethon

CMD ["python", "main.py"]
