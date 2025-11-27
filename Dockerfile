# Use a lightweight Python image
FROM python:3.11-slim

# Working directory inside the container
WORKDIR /app

# Ensure Python output is unbuffered (logs show up immediately)
ENV PYTHONUNBUFFERED=1

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your app (main.py, etc.)
COPY . .

# Default command to run the bot
CMD ["python", "main.py"]
