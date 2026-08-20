FROM python:3.12.1-slim-bookworm

WORKDIR /app

# FFmpeg + tgcrypto compilation ke liye required packages, + Node.js/npm
# (File Store Bot, filestore_bot/, isi container ke andar SAME repo se
# simultaneously chalta hai — koi alag Render service nahi chahiye).
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    gcc \
    g++ \
    make \
    python3-dev \
    libffi-dev \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# pip update
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application files
COPY . .

# File Store Bot (Node.js) dependencies
RUN cd filestore_bot && npm install --omit=dev

RUN chmod +x start.sh

ENV PORT=8000

EXPOSE 8000

# Production: start.sh dono bots ek saath launch karta hai (Node.js File
# Store bot background mein, Python gunicorn foreground mein).
CMD ["./start.sh"]
