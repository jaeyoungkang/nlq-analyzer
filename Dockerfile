FROM python:3.11-slim

WORKDIR /app

# 시스템 패키지 업데이트 및 필요한 도구 설치
RUN apt-get update && apt-get install -y \
    gcc \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Python 의존성 설치
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# 애플리케이션 파일 복사
COPY . .

# 환경변수 설정
ENV PYTHONPATH=/app
ENV FLASK_ENV=production

# 포트 설정
EXPOSE 8080

# 헬스체크
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Gunicorn으로 실행 (더 안전한 설정)
CMD exec gunicorn \
    --bind 0.0.0.0:8080 \
    --workers 1 \
    --threads 4 \
    --timeout 300 \
    --keep-alive 2 \
    --max-requests 1000 \
    --preload \
    web_profiler_app:app