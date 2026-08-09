FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py config.py .env.example ./
RUN mkdir -p data

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

CMD ["python", "bot.py"]
