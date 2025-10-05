# Use official Python image

FROM python:3.12-slim

# Set work directory

WORKDIR /app

# Install system dependencies

RUN apt-get update && apt-get install -y 
build-essential 
libssl-dev 
pkg-config 
curl 
&& rm -rf /var/lib/apt/lists/*

# Copy requirements first for caching

COPY requirements.txt .

# Install Python dependencies

RUN pip install --no-cache-dir -r requirements.txt

# Copy bot code

COPY . .

# Set env vars (can also be provided by Render dashboard)

ENV PYTHONUNBUFFERED=1

# Start the bot

CMD ["python", "solbot.py"]
