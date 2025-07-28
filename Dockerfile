# Python 3.11 이미지 사용
FROM python:3.11-slim

# 작업 디렉토리 설정
WORKDIR /app

# 시스템 의존성 설치
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Python 의존성 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 애플리케이션 코드 복사
COPY . .

# 환경변수 설정
ENV PYTHONPATH=/app
ENV FLASK_APP=app.py

# 포트 설정 (Cloud Run은 8080 포트 사용)
EXPOSE 8080

# Gunicorn으로 애플리케이션 실행
CMD exec gunicorn --bind :8080 --workers 1 --threads 8 --timeout 0 app:app