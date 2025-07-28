import os
import json
import time
import datetime
import re
import logging
from typing import List, Dict, Optional, Generator
from dotenv import load_dotenv
from flask import Flask, Response, render_template, request, jsonify
import anthropic
from google.cloud import bigquery
from google.cloud.exceptions import NotFound, BadRequest

# 데이터베이스 매니저 임포트
from firestore_db import db_manager

# --- 1. 설정 및 로깅 ---

# .env.local 파일에서 환경변수 로드
load_dotenv('.env.local')

# 환경변수에서 API 키 읽기 (보안 개선)
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY", "")

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Flask 웹 애플리케이션 초기화
app = Flask(__name__)

# --- 2. 백엔드 로직 및 전역 객체 ---

# Claude 클라이언트를 저장할 전역 변수
claude_client = None

class BigQueryAnalyzer:
    """BigQuery 데이터 분석을 위한 클래스"""
    
    def __init__(self, claude_client: anthropic.Anthropic):
        self.claude_client = claude_client
    
    def extract_bigquery_metadata(self, project_id: str, table_ids: List[str]) -> str:
        """BigQuery에서 메타데이터를 추출하여 LLM에 전달할 형식으로 컴파일합니다."""
        try:
            metadata_report = ["# BigQuery 데이터셋 메타데이터"]
            client = bigquery.Client(project=project_id)
            
            for table_id in table_ids:
                try:
                    table = client.get_table(table_id)
                    metadata_report.append(f"\n## 테이블: `{table_id}`")
                    metadata_report.append(f"- **총 행 개수**: {table.num_rows:,}")
                    metadata_report.append(f"- **생성일**: {table.created}")
                    metadata_report.append(f"- **수정일**: {table.modified}")
                    metadata_report.append(f"- **테이블 크기**: {table.num_bytes:,} bytes")
                    metadata_report.append("- **테이블 스키마**:")
                    
                    for field in table.schema:
                        if field.field_type == 'RECORD' and field.fields:
                            metadata_report.append(f"  - `{field.name}` (RECORD) - {field.description or 'No description'}")
                            for sub_field in field.fields:
                                metadata_report.append(f"    - `{sub_field.name}` ({sub_field.field_type}) - {sub_field.description or 'No description'}")
                        else:
                            metadata_report.append(f"  - `{field.name}` ({field.field_type}) - {field.description or 'No description'}")
                            
                except NotFound:
                    metadata_report.append(f"\n## 테이블: `{table_id}` - ❌ 테이블을 찾을 수 없습니다.")
                except Exception as e:
                    metadata_report.append(f"\n## 테이블: `{table_id}` - ❌ 오류: {str(e)}")
                    
            return "\n".join(metadata_report)
            
        except Exception as e:
            logger.error(f"메타데이터 추출 중 오류: {e}")
            raise Exception(f"BigQuery 메타데이터 추출 실패: {str(e)}")

    def generate_llm_response(self, prompt_text: str, max_retries: int = 3) -> str:
        """주어진 프롬프트로 Claude 모델을 호출하고 응답을 반환합니다."""
        for attempt in range(max_retries):
            try:
                message = self.claude_client.messages.create(
                    model="claude-3-5-sonnet-20241022",  # 최신 모델 사용
                    max_tokens=4096,
                    temperature=0.1,  # 일관성을 위해 낮은 온도 설정
                    messages=[
                        {"role": "user", "content": prompt_text}
                    ]
                )
                return message.content[0].text
            except Exception as e:
                logger.warning(f"Claude API 호출 시도 {attempt + 1} 실패: {e}")
                if attempt == max_retries - 1:
                    raise Exception(f"Claude API 호출 실패: {str(e)}")
                time.sleep(2 ** attempt)  # 지수 백오프

    def stream_profiling_process(self, project_id: str, table_ids: List[str]) -> Generator[str, None, None]:
        """
        전체 분석 프로세스를 실행하고 진행 상황을 스트리밍하며, 최종 결과를 로깅합니다.
        """
        log_entry = {
            "id": int(time.time() * 1000),
            "start_time": datetime.datetime.now().isoformat(),
            "end_time": None,
            "project_id": project_id,
            "table_ids": table_ids,
            "status": "진행 중",
            "error_message": None,
            "profiling_report": {
                "sections": {
                    "overview": "",
                    "table_analysis": "",
                    "relationships": "",
                    "business_questions": "",
                    "recommendations": ""
                },
                "full_report": ""
            },
            "sql_queries": []
        }
        
        # 세션 ID 초기화
        session_id = None
        
        def yield_sse_event(event_type: str, data: Dict) -> str:
            """Helper function to format and yield SSE messages."""
            payload = {"type": event_type, "payload": data}
            
            # Firestore에 로그 저장
            if session_id:
                if event_type in ['status', 'log', 'error']:
                    db_manager.add_log(
                        session_id, 
                        event_type, 
                        data.get('message', ''),
                        data
                    )
                elif event_type == 'report_section':
                    db_manager.add_log(
                        session_id,
                        'report_section',
                        f"프로파일링 섹션 완료: {data.get('title', '')}",
                        data
                    )
                elif event_type == 'sql_query':
                    db_manager.add_log(
                        session_id,
                        'sql_query',
                        f"SQL 쿼리 생성: {data.get('description', '')}",
                        data
                    )
            
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        try:
            # 입력 검증
            if not project_id or not table_ids or not all(table_ids):
                raise ValueError("GCP Project ID와 하나 이상의 Table ID를 입력해야 합니다.")
            
            # 데이터베이스에 세션 생성
            session_id = db_manager.create_analysis_session(log_entry)
            log_entry["session_id"] = session_id
                
            yield yield_sse_event("status", {"step": 0, "message": "Claude API 설정 확인 중..."})
            if self.claude_client is None:
                raise ValueError("Claude 클라이언트가 서버 시작 시 초기화되지 않았습니다. 서버 로그와 API 키를 확인해주세요.")
            yield yield_sse_event("log", {"message": "Claude API 설정 정상 확인."})
            yield yield_sse_event("status", {"step": 0, "message": "Claude API 설정 완료."})
            time.sleep(0.5)

            yield yield_sse_event("status", {"step": 1, "message": f"BigQuery에서 메타데이터 추출 중..."})
            yield yield_sse_event("log", {"message": f"대상 프로젝트: {project_id}"})
            yield yield_sse_event("log", {"message": f"분석 대상 테이블: {len(table_ids)}개"})
            
            metadata = self.extract_bigquery_metadata(project_id, table_ids)
            yield yield_sse_event("log", {"message": "모든 테이블의 메타데이터 추출 완료."})
            yield yield_sse_event("status", {"step": 1, "message": "메타데이터 추출 완료."})
            time.sleep(0.5)

            yield yield_sse_event("status", {"step": 2, "message": "LLM(Claude)을 통해 데이터 프로파일링 리포트 생성 중..."})
            
            # 실시간으로 섹션별 분석 결과 스트리밍
            sections = [
                ("overview", "개요 분석 중...", "데이터셋의 전반적인 특성과 규모를 분석하고 각 테이블의 주요 특징을 요약해주세요."),
                ("table_analysis", "테이블 상세 분석 중...", "각 테이블별 세부 정보, 스키마 구조 분석, 데이터 품질 고려사항을 분석해주세요."),
                ("relationships", "테이블 관계 추론 중...", "공통 필드나 연결 가능한 키를 식별하고 데이터 플로우를 추정해주세요."),
                ("business_questions", "비즈니스 질문 도출 중...", "이 데이터로 답할 수 있는 핵심 비즈니스 질문 5개와 각 질문에 대한 분석 접근 방법을 제시해주세요."),
                ("recommendations", "활용 권장사항 도출 중...", "효과적인 데이터 활용 방안과 주의사항 및 제한사항을 제시해주세요.")
            ]
            
            full_report_parts = ["# 데이터 프로파일링 리포트\n"]
            
            for section_key, section_message, section_prompt in sections:
                yield yield_sse_event("log", {"message": section_message})
                
                full_prompt = f"""
                당신은 Google Cloud 전문 데이터 분석가입니다. 다음은 BigQuery 데이터셋의 메타데이터입니다.

                {metadata}

                위 메타데이터를 기반으로 다음 작업을 수행해주세요:
                {section_prompt}

                한국어로 상세하고 전문적으로 작성해주세요.
                """
                
                section_content = self.generate_llm_response(full_prompt)
                log_entry["profiling_report"]["sections"][section_key] = section_content
                
                # 섹션별 실시간 스트리밍
                yield yield_sse_event("report_section", {
                    "section": section_key,
                    "title": section_message.replace(" 중...", ""),
                    "content": section_content
                })
                
                # 전체 리포트에 추가
                section_titles = {
                    "overview": "## 1. 개요",
                    "table_analysis": "## 2. 테이블 상세 분석", 
                    "relationships": "## 3. 테이블 간의 관계 추론",
                    "business_questions": "## 4. 주요 분석 가능 질문 (비즈니스 질문)",
                    "recommendations": "## 5. 데이터 활용 권장사항"
                }
                full_report_parts.append(f"{section_titles[section_key]}\n{section_content}\n")
                
                time.sleep(0.3)  # 각 섹션 간 짧은 대기
            
            # 전체 리포트 완성
            full_report = "\n".join(full_report_parts)
            log_entry["profiling_report"]["full_report"] = full_report
            
            # 프로파일링 리포트 저장
            db_manager.save_analysis_result(session_id, 'profiling_report', log_entry["profiling_report"])
            
            yield yield_sse_event("log", {"message": "모든 프로파일링 섹션 완료."})
            yield yield_sse_event("status", {"step": 2, "message": "프로파일링 리포트 생성 완료."})
            time.sleep(0.5)

            yield yield_sse_event("status", {"step": 3, "message": "생성된 리포트를 기반으로 핵심 SQL 쿼리 생성 중..."})
            
            # 비즈니스 질문에서 개별 질문 추출
            business_questions_content = log_entry["profiling_report"]["sections"]["business_questions"]
            
            # SQL 쿼리를 개별적으로 생성하여 실시간 스트리밍
            query_prompts = [
                "데이터의 전반적인 현황과 트렌드를 파악할 수 있는 기본 통계 쿼리",
                "각 테이블의 데이터 품질과 완성도를 확인하는 데이터 품질 검증 쿼리", 
                "테이블 간 관계를 활용한 조인 분석 쿼리",
                "시계열 데이터가 있다면 시간별 트렌드 분석 쿼리",
                "비즈니스 KPI나 핵심 메트릭을 계산하는 쿼리"
            ]
            
            for i, query_description in enumerate(query_prompts, 1):
                yield yield_sse_event("log", {"message": f"SQL 쿼리 {i}/5 생성 중: {query_description}"})
                
                sql_prompt = f"""
                당신은 BigQuery SQL 전문가입니다. 다음 조건에 맞는 BigQuery 표준 SQL 쿼리를 생성해주세요:

                **메타데이터:**
                {metadata}

                **비즈니스 컨텍스트:**
                {business_questions_content}

                **생성할 쿼리:**
                {query_description}

                **요구사항:**
                1. 쿼리 위에 한국어로 상세한 주석을 달아주세요
                2. 실행 가능한 완전한 형태의 쿼리를 작성해주세요
                3. RECORD 타입이 있다면 UNNEST를 적절히 사용해주세요
                4. 성능을 고려한 최적화된 쿼리를 작성해주세요
                5. 쿼리는 ```sql로 시작하고 ```로 끝나는 코드 블록으로 작성해주세요

                하나의 쿼리만 생성해주세요.
                """
                
                sql_query = self.generate_llm_response(sql_prompt)
                
                query_entry = {
                    "id": i,
                    "description": query_description,
                    "query": sql_query,
                    "created_at": datetime.datetime.now().isoformat()
                }
                
                log_entry["sql_queries"].append(query_entry)
                
                # 개별 쿼리 실시간 스트리밍
                yield yield_sse_event("sql_query", {
                    "query_id": i,
                    "description": query_description,
                    "content": sql_query
                })
                
                time.sleep(0.3)  # 각 쿼리 간 짧은 대기
            
            # SQL 쿼리 결과 저장
            db_manager.save_analysis_result(session_id, 'sql_queries', log_entry["sql_queries"])
            
            yield yield_sse_event("log", {"message": "모든 SQL 쿼리 생성 완료."})
            yield yield_sse_event("status", {"step": 3, "message": "SQL 쿼리 생성 완료."})
            time.sleep(0.5)

            yield yield_sse_event("status", {"step": 4, "message": "모든 분석 프로세스가 완료되었습니다."})
            log_entry["status"] = "완료"
            db_manager.update_session_status(session_id, "완료")

        except Exception as e:
            logger.error(f"분석 프로세스 중 오류: {e}")
            log_entry["status"] = "실패"
            log_entry["error_message"] = str(e)
            if session_id:
                db_manager.update_session_status(session_id, "실패", str(e))
            yield yield_sse_event("error", {"message": f"오류 발생: {str(e)}"})
        
        finally:
            log_entry["end_time"] = datetime.datetime.now().isoformat()


# --- 애플리케이션 시작 시 API 설정 ---
def initialize_claude_client() -> Optional[anthropic.Anthropic]:
    """Claude API 클라이언트를 초기화합니다."""
    try:
        logger.info("Claude API 설정 시도 중...")
        if not CLAUDE_API_KEY:
            logger.warning("경고: CLAUDE_API_KEY가 설정되지 않았습니다. 환경변수를 확인해주세요.")
            return None
        else:
            client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
            logger.info("Claude API가 성공적으로 설정되었습니다.")
            return client
    except Exception as e:
        logger.error(f"치명적 오류: Claude API 설정에 실패했습니다: {e}")
        logger.error("API 키 또는 'anthropic' 라이브러리 설치를 확인하세요.")
        return None

claude_client = initialize_claude_client()
analyzer = BigQueryAnalyzer(claude_client) if claude_client else None

# --- 3. Flask 라우트 설정 ---

@app.route('/')
def index():
    """메인 HTML 페이지를 렌더링합니다."""
    return render_template('index.html')

@app.route('/run-profiler')
def run_profiler():
    """분석 프로세스를 시작하고 진행 상황을 스트리밍하는 SSE 엔드포인트입니다."""
    if not analyzer:
        def error_generator():
            yield f"data: {json.dumps({'type': 'error', 'payload': {'message': 'Claude API가 초기화되지 않았습니다. 환경변수 CLAUDE_API_KEY를 확인해주세요.'}}, ensure_ascii=False)}\n\n"
        return Response(error_generator(), mimetype='text/event-stream')
    
    project_id = request.args.get('projectId', '').strip()
    table_ids_str = request.args.get('tableIds', '').strip()
    
    # 입력 검증
    if not project_id:
        def error_generator():
            yield f"data: {json.dumps({'type': 'error', 'payload': {'message': 'GCP Project ID가 필요합니다.'}}, ensure_ascii=False)}\n\n"
        return Response(error_generator(), mimetype='text/event-stream')
    
    if not table_ids_str:
        def error_generator():
            yield f"data: {json.dumps({'type': 'error', 'payload': {'message': '최소 하나의 Table ID가 필요합니다.'}}, ensure_ascii=False)}\n\n"
        return Response(error_generator(), mimetype='text/event-stream')
    
    # 테이블 ID 파싱
    table_ids = re.split(r'[,\n]+', table_ids_str)
    table_ids = [tid.strip() for tid in table_ids if tid.strip()]
    
    if not table_ids:
        def error_generator():
            yield f"data: {json.dumps({'type': 'error', 'payload': {'message': '유효한 Table ID가 없습니다.'}}, ensure_ascii=False)}\n\n"
        return Response(error_generator(), mimetype='text/event-stream')
    
    logger.info(f"분석 시작: Project={project_id}, Tables={len(table_ids)}개")
    
    return Response(
        analyzer.stream_profiling_process(project_id, table_ids), 
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'Access-Control-Allow-Origin': '*'
        }
    )

@app.route('/logs')
def get_logs():
    """저장된 분석 작업 기록을 반환합니다."""
    try:
        page = int(request.args.get('page', 1))
        limit = int(request.args.get('limit', 20))
        project_id = request.args.get('project_id')
        
        logs = db_manager.get_analysis_sessions(limit=limit, project_id=project_id)
        
        logger.info(f"기록 조회: {len(logs)}개 항목")
        return jsonify({
            "logs": logs,
            "page": page,
            "limit": limit,
            "total": len(logs)
        })
    except Exception as e:
        logger.error(f"기록 조회 중 오류: {e}")
        return jsonify({"error": "기록을 불러오는 중 오류가 발생했습니다."}), 500

@app.route('/logs/<session_id>')
def get_log_detail(session_id):
    """특정 세션의 상세 정보와 로그를 반환합니다."""
    try:
        include_logs = request.args.get('include_logs', 'true').lower() == 'true'
        log = db_manager.get_analysis_session_with_logs(session_id, include_logs)
        if not log:
            return jsonify({"error": "세션을 찾을 수 없습니다."}), 404
        return jsonify(log)
    except Exception as e:
        logger.error(f"세션 조회 중 오류: {e}")
        return jsonify({"error": "세션 조회 중 오류가 발생했습니다."}), 500

@app.route('/logs/<session_id>/logs')
def get_session_logs(session_id):
    """특정 세션의 로그만 조회합니다."""
    try:
        log_type = request.args.get('type')  # status, log, error, report_section, sql_query
        limit = int(request.args.get('limit', 100))
        
        logs = db_manager.get_session_logs(session_id, log_type, limit)
        return jsonify({
            "session_id": session_id,
            "logs": logs,
            "count": len(logs)
        })
    except Exception as e:
        logger.error(f"로그 조회 중 오류: {e}")
        return jsonify({"error": "로그 조회 중 오류가 발생했습니다."}), 500

@app.route('/logs/<session_id>', methods=['DELETE'])
def delete_log(session_id):
    """분석 세션을 삭제합니다."""
    try:
        success = db_manager.delete_analysis_session(session_id)
        if success:
            return jsonify({"message": "세션이 삭제되었습니다."})
        else:
            return jsonify({"error": "세션을 찾을 수 없습니다."}), 404
    except Exception as e:
        logger.error(f"세션 삭제 중 오류: {e}")
        return jsonify({"error": "세션 삭제 중 오류가 발생했습니다."}), 500

@app.route('/favorites', methods=['POST'])
def add_favorite():
    """즐겨찾기 추가"""
    try:
        data = request.get_json()
        session_id = data.get('session_id')
        query_id = data.get('query_id')
        name = data.get('name')
        description = data.get('description', '')
        
        favorite_id = db_manager.add_to_favorites(session_id, query_id, name, description)
        return jsonify({"id": favorite_id, "message": "즐겨찾기에 추가되었습니다."})
    except Exception as e:
        logger.error(f"즐겨찾기 추가 중 오류: {e}")
        return jsonify({"error": "즐겨찾기 추가 중 오류가 발생했습니다."}), 500

@app.route('/favorites')
def get_favorites():
    """즐겨찾기 목록 조회"""
    try:
        favorites = db_manager.get_favorites()
        return jsonify(favorites)
    except Exception as e:
        logger.error(f"즐겨찾기 조회 중 오류: {e}")
        return jsonify({"error": "즐겨찾기 조회 중 오류가 발생했습니다."}), 500

@app.route('/stats')
def get_stats():
    """통계 정보 조회"""
    try:
        stats = db_manager.get_project_stats()
        return jsonify(stats)
    except Exception as e:
        logger.error(f"통계 조회 중 오류: {e}")
        return jsonify({"error": "통계 조회 중 오류가 발생했습니다."}), 500

@app.route('/search')
def search_sessions():
    """세션 검색"""
    try:
        keyword = request.args.get('q', '')
        if not keyword:
            return jsonify({"error": "검색어가 필요합니다."}), 400
        
        results = db_manager.search_sessions(keyword)
        return jsonify(results)
    except Exception as e:
        logger.error(f"검색 중 오류: {e}")
        return jsonify({"error": "검색 중 오류가 발생했습니다."}), 500

@app.route('/health')
def health_check():
    """서버 상태 확인 엔드포인트"""
    status = {
        "status": "healthy",
        "claude_api": "available" if claude_client else "unavailable",
        "firestore": "available" if db_manager.db else "unavailable",
        "timestamp": datetime.datetime.now().isoformat()
    }
    return jsonify(status)

@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "페이지를 찾을 수 없습니다."}), 404

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"내부 서버 오류: {error}")
    return jsonify({"error": "내부 서버 오류가 발생했습니다."}), 500

if __name__ == '__main__':
    logger.info("BigQuery 데이터 분석기 서버 시작")
    logger.info(f"Claude API 상태: {'사용 가능' if claude_client else '사용 불가'}")
    logger.info(f"Firestore 상태: {'사용 가능' if db_manager.db else '사용 불가'}")
    
    # 포트 설정 (Cloud Run은 8080, 로컬은 5001)
    port = int(os.getenv('PORT', 5001))
    app.run(debug=True, threaded=True, host='0.0.0.0', port=port)