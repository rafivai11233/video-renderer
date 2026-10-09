# RAFI video-renderer - local render in Docker (free, no GitHub needed)
# Usage: docker build -t rafi-render .
#        docker run --rm -v $PWD/out:/app/out \
#          -e PLAN='{"title":"...","scenes":[...]}' \
#          -e GEMINI_API_KEY=your_key rafi-render
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu
RUN pip install --no-cache-dir requests edge-tts

WORKDIR /app
COPY render.py ./
COPY music/ ./music/

# PLAN env must contain the plan JSON (same as n8n sends to GitHub Actions)
ENV PYTHONUNBUFFERED=1
CMD python render.py
