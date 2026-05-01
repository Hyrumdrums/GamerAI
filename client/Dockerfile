FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY client/requirements.txt ./client/requirements.txt
RUN pip install --no-cache-dir -r client/requirements.txt

COPY shared/ ./shared/
COPY client/ ./client/

EXPOSE 8080

CMD ["uvicorn", "client.web:app", "--host", "0.0.0.0", "--port", "8080"]
