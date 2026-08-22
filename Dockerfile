FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV HOST=0.0.0.0 PORT=8080
EXPOSE 8080
VOLUME ["/app/data"]

CMD ["sh", "-c", "uvicorn app.main:app --host ${HOST} --port ${PORT}"]
