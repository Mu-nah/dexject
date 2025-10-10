# Use official Python image
FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Copy requirements first for caching
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the bot code
COPY . .

# Expose the port (Render uses $PORT env variable)
EXPOSE 5000

# Command to run the bot
CMD ["python", "solbotA.py"]
