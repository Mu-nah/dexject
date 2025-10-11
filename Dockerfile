# === Base image ===
FROM python:3.11-slim

# === Environment setup ===
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# === Working directory ===
WORKDIR /app

# === Install system dependencies ===
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# === Copy project files ===
COPY . .

# === Install Python dependencies ===
RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# === Expose port for Render ===
EXPOSE 5000

# === Run the bot with Flask server ===
CMD ["python", "solbot.py"]
