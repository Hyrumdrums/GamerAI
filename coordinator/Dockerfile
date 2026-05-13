FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY coordinator/requirements.txt ./coordinator/requirements.txt
RUN pip install --no-cache-dir -r coordinator/requirements.txt

COPY shared/ ./shared/
COPY coordinator/ ./coordinator/
# The /tos endpoint serves docs/community-tos.md. Copying just the
# one file keeps the image lean while still letting us bump the ToS
# by editing the markdown.
COPY docs/community-tos.md ./docs/community-tos.md

EXPOSE 8000

CMD ["uvicorn", "coordinator.main:app", "--host", "0.0.0.0", "--port", "8000"]
