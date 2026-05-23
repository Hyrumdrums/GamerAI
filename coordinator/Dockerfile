FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# NudeNet (and onnxruntime under the hood) needs libGL + libglib for
# OpenCV's image I/O, and curl for the model warm-up below. -slim
# omits these by default; install them once at the top so subsequent
# layers stay deterministic.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 curl && \
    rm -rf /var/lib/apt/lists/*

COPY coordinator/requirements.txt ./coordinator/requirements.txt
RUN pip install --no-cache-dir -r coordinator/requirements.txt

# Warm up the NudeNet model cache so the first image submission after
# a deploy doesn't pay the ~50 MB download tax (and so deploys are
# repeatable when the upstream model host has a bad day). NudeDetector
# downloads to ~/.NudeNet on first construction — instantiating it at
# build time bakes the model into the image.
RUN python -c "from nudenet import NudeDetector; NudeDetector()" \
    || echo "nudenet warm-up failed; classifier will lazy-load at runtime"

COPY shared/ ./shared/
COPY coordinator/ ./coordinator/
# The /tos endpoint serves docs/community-tos.md. Copying just the
# one file keeps the image lean while still letting us bump the ToS
# by editing the markdown.
COPY docs/community-tos.md ./docs/community-tos.md

EXPOSE 8000

CMD ["uvicorn", "coordinator.main:app", "--host", "0.0.0.0", "--port", "8000"]
