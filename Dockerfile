FROM python:3.11-slim

WORKDIR /app

# 시스템 패키지
RUN apt-get update && apt-get install -y \
    gcc \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Python 패키지 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 소스 코드 복사
COPY . .

# 데이터 디렉토리 (Multi-Agent 전용)
RUN mkdir -p /app/data/multi/chromadb /app/data/multi/db /app/logs

# 환경변수 (OOSDK 실험 버전)
ENV MCP_MODE=sse
ENV HOST=0.0.0.0
ENV PORT=9100
ENV MCP_PORT=9100
ENV LOG_API_PORT=9101
ENV CHROMA_PERSIST_DIR=/app/data/multi/chromadb

# 포트 노출 (Multi-Agent OOSDK: 9100 MCP, 9101 Log API)
EXPOSE 9100 9101

CMD ["python", "mcp_server/server.py"]
