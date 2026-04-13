FROM python:3.11-slim

LABEL maintainer="Kwanso <info@kwanso.com>"
LABEL description="AI-powered PR code review agent using LangGraph and RAG"

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml .
COPY app/ ./app/

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e .

# Create data directory for checkpoints
RUN mkdir -p /app/data

# Expose port
EXPOSE 3400

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:3400/health')" || exit 1

# Run application
CMD ["python", "-m", "app.server"]
