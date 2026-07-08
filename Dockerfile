FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libjpeg62-turbo \
        zlib1g \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py config.py level_up_card.py voice_rooms.py .env.example ./
RUN mkdir -p data

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

CMD ["python", "bot.py"]
