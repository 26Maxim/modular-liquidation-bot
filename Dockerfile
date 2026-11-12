FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    make \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install Foundry
ENV FOUNDRY_DIR=/opt/foundry
ENV PATH="/opt/foundry/bin:${PATH}"
RUN mkdir -p /opt/foundry && \
    curl -sSL https://foundry.paradigm.xyz | bash && \
    /opt/foundry/bin/foundryup

WORKDIR /workspace

COPY pyproject.toml .
COPY backend ./backend
COPY contracts ./contracts
COPY README.md .

RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir ".[dev]"

COPY . .

ENV PYTHONPATH="/workspace/backend/src"

EXPOSE 8090

CMD ["uvicorn", "liquidator.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8090"]
