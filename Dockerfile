FROM mcr.microsoft.com/playwright/python:v1.48.0-noble

WORKDIR /app

# Install xvfb, xauth, and procps so virtual X11 display works in container
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    xauth \
    procps \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/static/uploads /app/fb_profile

ENV PORT=10000
ENV HOST=0.0.0.0
ENV DISPLAY=:99
ENV PYTHONUNBUFFERED=1

EXPOSE 10000

# Start Xvfb in the background and immediately bind Flask to 0.0.0.0:$PORT
CMD ["sh", "-c", "rm -f /tmp/.X99-lock && (Xvfb :99 -screen 0 1280x880x24 -ac +extension GLX +render -noreset >/dev/null 2>&1 &) && exec python -u app.py"]
