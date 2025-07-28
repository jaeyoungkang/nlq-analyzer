# GCP 배포 가이드

## 🚀 빠른 배포 (Cloud Run)

### 1. GCP 프로젝트 설정
```bash
# GCP CLI 설치 및 로그인
gcloud auth login
gcloud config set project YOUR_PROJECT_ID

# 필요한 API 활성화
gcloud services enable run.googleapis.com
gcloud services enable cloudbuild.googleapis.com
gcloud services enable firestore.googleapis.com
gcloud services enable bigquery.googleapis.com
```

### 2. Firestore 설정
```bash
# Firestore 데이터베이스 생성 (Native 모드)
gcloud firestore databases create --region=asia-northeast3
```

### 3. 환경변수 설정
```bash
# Secret Manager에 API 키 저장
echo "your-claude-api-key" | gcloud secrets create claude-api-key --data-file=-

# Cloud Run 서비스 계정에 권한 부여
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
    --member="serviceAccount:YOUR_PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor"
```

### 4. 배포 실행
```bash
# Cloud Build로 자동 배포
gcloud builds submit --config cloudbuild.yaml

# 또는 직접 배포
gcloud run deploy bigquery-analyzer \
    --source . \
    --region asia-northeast3 \
    --allow-unauthenticated \
    --set-env-vars CLAUDE_API_KEY="your-claude-api-key"
```

## 📊 주요 기능

### 실시간 로그 저장
- 모든 분석 과정이 Firestore에 실시간 저장
- 세션별 상세 로그 추적 가능
- 에러 발생 시 완전한 디버깅 정보

### API 엔드포인트
```
GET  /logs                           # 세션 목록
GET  /logs/{session_id}              # 세션 상세 (로그 포함)
GET  /logs/{session_id}/logs         # 세션의 로그만
GET  /logs/{session_id}/logs?type=error  # 특정 타입 로그만
POST /favorites                      # 즐겨찾기 추가
GET  /stats                         # 전체 통계
```

### 로그 타입
- `system`: 시스템 이벤트
- `status`: 진행 상태 업데이트  
- `log`: 일반 로그 메시지
- `error`: 에러 발생
- `report_section`: 리포트 섹션 완료
- `sql_query`: SQL 쿼리 생성

## 🔧 모니터링

### Cloud Logging
```bash
# 애플리케이션 로그 확인
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=bigquery-analyzer"
```

### Firestore 데이터 구조
```
/analysis_sessions/{session_id}
├── session_data (문서)
└── /logs/{log_id} (서브컬렉션)
    ├── timestamp
    ├── log_type  
    ├── message
    └── metadata
```

## 💰 비용 최적화

### Firestore 비용
- 읽기: 100,000회/일 무료
- 쓰기: 20,000회/일 무료
- 삭제: 20,000회/일 무료

### Cloud Run 비용
- CPU: vCPU당 $0.00002400/초
- 메모리: GB당 $0.00000250/초
- 요청: 200만 요청/월 무료

### 권장 설정
```yaml
resources:
  limits:
    cpu: 2
    memory: 2Gi
  requests:
    cpu: 1
    memory: 1Gi
```

## 🔒 보안 설정

### IAM 권한
```bash
# 서비스 계정에 최소 권한 부여
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
    --member="serviceAccount:YOUR_SERVICE_ACCOUNT" \
    --role="roles/firestore.user"

gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
    --member="serviceAccount:YOUR_SERVICE_ACCOUNT" \
    --role="roles/bigquery.user"
```

### 네트워크 보안
```bash
# Cloud Run 서비스에 VPC 커넥터 설정 (선택사항)
gcloud compute networks vpc-access connectors create bigquery-analyzer-connector \
    --region=asia-northeast3 \
    --subnet=default \
    --subnet-project=YOUR_PROJECT_ID
```