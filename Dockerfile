FROM mcr.microsoft.com/playwright/python:v1.48.0-noble

WORKDIR /app

# Install xvfb so headed Chromium (headless=False) works in a virtual X11 display on Render
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    procps \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && playwright install chromium

COPY . .
RUN mkdir -p /app/static/uploads /app/fb_profile

ENV PORT=10000
ENV HOST=0.0.0.0
ENV PYTHONUNBUFFERED=1

EXPOSE 10000

CMD ["xvfb-run", "-a", "--server-args=-screen 0 1280x880x24", "python", "-u", "app.py"]
