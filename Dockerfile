FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py chat.py top_us_airports.csv us_cities.csv ./

ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "exec gunicorn --bind :${PORT} --workers 1 --threads 100 --timeout 0 --access-logfile - --error-logfile - app:app"]
