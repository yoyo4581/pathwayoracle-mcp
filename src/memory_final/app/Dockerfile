FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc g++ && \
    rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy server code
COPY . /app

# Create data directory for persistence
RUN mkdir -p /app/data

# Expose port if using SSE (optional)
EXPOSE 8000

# Run the FastMCP server
CMD ["python", "server.py"]