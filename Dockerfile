# Use official slim Python
FROM python:3.11-slim

# set environment
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# system deps for some crypto libs (solders/solana may need build deps)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libssl-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# copy requirements first for layer caching
COPY requirements.txt .

# install
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# copy app
COPY . /app

# expose port used by Render (use $PORT at runtime)
EXPOSE 8080

# Use a single worker to avoid duplicate bot instances; Render sets $PORT
CMD ["sh", "-c", "gunicorn 'app:flask_app' --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 4"]
