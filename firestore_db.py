import os
import json
import logging
from datetime import datetime
from typing import List, Dict, Optional, Generator
from google.cloud import firestore
from google.api_core.exceptions import NotFound

logger = logging.getLogger(__name__)

class FirestoreManager:
    """Firestore 데이터베이스 관리자 (로그 포함)"""
    
    def __init__(self):
        try:
            # Firestore 클라이언트 초기화 (자동으로 GCP 인증 사용)
            self.db = firestore.Client()
            logger.info("Firestore 연결이 성공적으로 설정되었습니다.")
        except Exception as e:
            logger.error(f"Firestore 연결 실패: {e}")
            # 로컬 개발환경용 더미 클래스
            self.db = None
    
    def create_analysis_session(self, session_data: Dict) -> str:
        """새 분석 세션 생성"""
        if not self.db:
            return session_data.get('id', str(datetime.now().timestamp()))
    
        try:
            session_id = session_data.get('id', str(datetime.now().timestamp()).replace('.', ''))
            
            clean_data = self._sanitize_data(session_data)
            # 세션 메타데이터 저장
            session_ref = self.db.collection('analysis_sessions').document(session_id)
            session_ref.set({
                'session_id': session_id,
                'project_id': clean_data['project_id'],
                'table_ids': clean_data['table_ids'],
                'status': clean_data['status'],
                'start_time': clean_data['start_time'],
                'end_time': clean_data.get('end_time', ''),
                'error_message': clean_data.get('error_message', ''),
                'created_at': firestore.SERVER_TIMESTAMP,
                'log_count': 0,
                'last_updated': firestore.SERVER_TIMESTAMP
            })
            
            # 초기 로그 추가
            self.add_log(session_id, 'system', '분석 세션이 시작되었습니다.', {'step': 0})
            
            logger.info(f"새 세션 {session_id} 생성 완료")
            return session_id
            
        except Exception as e:
            logger.error(f"세션 생성 중 오류: {e}")
            raise
    
    def add_log(self, session_id: str, log_type: str, message: str, metadata: Dict = None) -> str:
        """세션에 로그 추가"""
        if not self.db:
            return ""
        
        try:
            timestamp = datetime.now()
            log_id = f"{timestamp.strftime('%Y%m%d_%H%M%S_%f')}_{log_type}"
            
            # 로그 데이터 준비
            log_data = {
                'session_id': session_id,
                'log_type': log_type,  # 'status', 'log', 'error', 'report_section', 'sql_query'
                'message': message,
                'timestamp': timestamp.isoformat(),
                'firestore_timestamp': firestore.SERVER_TIMESTAMP,
                'metadata': metadata or {}
            }
            
            # 서브컬렉션에 로그 추가
            log_ref = self.db.collection('analysis_sessions').document(session_id)\
                           .collection('logs').document(log_id)
            log_ref.set(log_data)
            
            # 세션 문서의 로그 카운트 업데이트
            session_ref = self.db.collection('analysis_sessions').document(session_id)
            session_ref.update({
                'log_count': firestore.Increment(1),
                'last_updated': firestore.SERVER_TIMESTAMP,
                'latest_log': message[:100]  # 최신 로그 미리보기
            })
            
            return log_id
            
        except Exception as e:
            logger.error(f"로그 추가 중 오류: {e}")
            return ""
    
    def save_analysis_result(self, session_id: str, result_type: str, data: Dict) -> bool:
        """분석 결과 저장 (프로파일링 리포트, SQL 쿼리 등)"""
        if not self.db:
            return False
        
        try:
            session_ref = self.db.collection('analysis_sessions').document(session_id)
            
            # 결과 타입에 따라 다른 필드에 저장
            update_data = {
                'last_updated': firestore.SERVER_TIMESTAMP
            }
            
            if result_type == 'profiling_report':
                update_data['profiling_report'] = data
                self.add_log(session_id, 'result', f'프로파일링 리포트 저장 완료', 
                           {'sections_count': len(data.get('sections', {}))})
            elif result_type == 'sql_queries':
                update_data['sql_queries'] = data
                self.add_log(session_id, 'result', f'SQL 쿼리 {len(data)}개 저장 완료',
                           {'queries_count': len(data)})
            
            session_ref.update(update_data)
            return True
            
        except Exception as e:
            logger.error(f"분석 결과 저장 중 오류: {e}")
            return False
    
    def update_session_status(self, session_id: str, status: str, error_message: str = None) -> bool:
        """세션 상태 업데이트"""
        if not self.db:
            return False
        
        try:
            session_ref = self.db.collection('analysis_sessions').document(session_id)
            
            update_data = {
                'status': status,
                'last_updated': firestore.SERVER_TIMESTAMP
            }
            
            if status == '완료':
                update_data['end_time'] = datetime.now().isoformat()
                self.add_log(session_id, 'system', '분석이 성공적으로 완료되었습니다.')
            elif status == '실패':
                update_data['end_time'] = datetime.now().isoformat()
                update_data['error_message'] = error_message
                self.add_log(session_id, 'error', f'분석 실패: {error_message}')
            
            session_ref.update(update_data)
            return True
            
        except Exception as e:
            logger.error(f"세션 상태 업데이트 중 오류: {e}")
            return False
    
    def get_analysis_sessions(self, limit: int = 50, project_id: Optional[str] = None) -> List[Dict]:
        """분석 세션 목록 조회"""
        if not self.db:
            return []
        
        try:
            query = self.db.collection('analysis_sessions')
            
            if project_id:
                query = query.where('project_id', '==', project_id)
            
            query = query.order_by('created_at', direction=firestore.Query.DESCENDING).limit(limit)
            
            docs = query.stream()
            
            sessions = []
            for doc in docs:
                data = doc.to_dict()
                data['id'] = doc.id
                sessions.append(data)
            
            logger.info(f"세션 {len(sessions)}개 조회 완료")
            return sessions
            
        except Exception as e:
            logger.error(f"세션 조회 중 오류: {e}")
            return []
    
    def get_analysis_session_with_logs(self, session_id: str, include_logs: bool = True) -> Optional[Dict]:
        """세션과 로그를 함께 조회"""
        if not self.db:
            return None
        
        try:
            # 세션 데이터 조회
            session_ref = self.db.collection('analysis_sessions').document(session_id)
            session_doc = session_ref.get()
            
            if not session_doc.exists:
                return None
            
            session_data = session_doc.to_dict()
            session_data['id'] = session_doc.id
            
            if include_logs:
                # 로그 조회
                logs_query = session_ref.collection('logs')\
                                      .order_by('firestore_timestamp', direction=firestore.Query.ASCENDING)
                
                logs = []
                for log_doc in logs_query.stream():
                    log_data = log_doc.to_dict()
                    log_data['id'] = log_doc.id
                    logs.append(log_data)
                
                session_data['logs'] = logs
                session_data['logs_count'] = len(logs)
            
            return session_data
            
        except Exception as e:
            logger.error(f"세션 상세 조회 중 오류: {e}")
            return None
    
    def get_session_logs(self, session_id: str, log_type: Optional[str] = None, limit: int = 100) -> List[Dict]:
        """특정 세션의 로그만 조회"""
        if not self.db:
            return []
        
        try:
            logs_ref = self.db.collection('analysis_sessions').document(session_id).collection('logs')
            
            query = logs_ref.order_by('firestore_timestamp', direction=firestore.Query.ASCENDING)
            
            if log_type:
                query = query.where('log_type', '==', log_type)
            
            if limit:
                query = query.limit(limit)
            
            logs = []
            for doc in query.stream():
                log_data = doc.to_dict()
                log_data['id'] = doc.id
                logs.append(log_data)
            
            return logs
            
        except Exception as e:
            logger.error(f"로그 조회 중 오류: {e}")
            return []
    
    def delete_analysis_session(self, session_id: str) -> bool:
        """분석 세션과 모든 로그 삭제"""
        if not self.db:
            return False
        
        try:
            session_ref = self.db.collection('analysis_sessions').document(session_id)
            
            # 로그 서브컬렉션 삭제
            logs_ref = session_ref.collection('logs')
            logs = logs_ref.stream()
            
            batch = self.db.batch()
            for log_doc in logs:
                batch.delete(log_doc.reference)
            
            # 세션 문서 삭제
            batch.delete(session_ref)
            batch.commit()
            
            logger.info(f"세션 {session_id}와 모든 로그 삭제 완료")
            return True
            
        except Exception as e:
            logger.error(f"세션 삭제 중 오류: {e}")
            return False
    
    def add_to_favorites(self, session_id: str, query_id: int, name: str, description: str = "") -> str:
        """즐겨찾기 추가"""
        if not self.db:
            return ""
        
        try:
            doc_ref = self.db.collection('favorites').add({
                'session_id': session_id,
                'query_id': query_id,
                'name': name,
                'description': description,
                'created_at': firestore.SERVER_TIMESTAMP
            })
            
            favorite_id = doc_ref[1].id
            self.add_log(session_id, 'system', f'쿼리 {query_id}를 즐겨찾기에 추가했습니다.')
            return favorite_id
            
        except Exception as e:
            logger.error(f"즐겨찾기 추가 중 오류: {e}")
            return ""
    
    def get_favorites(self, limit: int = 20) -> List[Dict]:
        """즐겨찾기 목록 조회"""
        if not self.db:
            return []
        
        try:
            query = self.db.collection('favorites')\
                          .order_by('created_at', direction=firestore.Query.DESCENDING)\
                          .limit(limit)
            
            docs = query.stream()
            
            favorites = []
            for doc in docs:
                data = doc.to_dict()
                data['id'] = doc.id
                favorites.append(data)
            
            return favorites
            
        except Exception as e:
            logger.error(f"즐겨찾기 조회 중 오류: {e}")
            return []
    
    def get_project_stats(self) -> Dict:
        """프로젝트 통계 조회"""
        if not self.db:
            return {}
        
        try:
            # 전체 세션 수
            all_sessions = self.db.collection('analysis_sessions').stream()
            
            total_sessions = 0
            unique_projects = set()
            completed_sessions = 0
            failed_sessions = 0
            total_logs = 0
            
            for doc in all_sessions:
                data = doc.to_dict()
                total_sessions += 1
                unique_projects.add(data.get('project_id', ''))
                total_logs += data.get('log_count', 0)
                
                status = data.get('status', '')
                if status == '완료':
                    completed_sessions += 1
                elif status == '실패':
                    failed_sessions += 1
            
            return {
                'total_sessions': total_sessions,
                'unique_projects': len(unique_projects),
                'completed_sessions': completed_sessions,
                'failed_sessions': failed_sessions,
                'total_logs': total_logs,
                'success_rate': round(completed_sessions / total_sessions * 100, 1) if total_sessions > 0 else 0
            }
            
        except Exception as e:
            logger.error(f"통계 조회 중 오류: {e}")
            return {}
    
    def search_sessions(self, keyword: str, limit: int = 20) -> List[Dict]:
        """세션 검색 (프로젝트 ID 기반)"""
        if not self.db:
            return []
        
        try:
            # Firestore는 부분 문자열 검색이 제한적이므로 prefix 검색 사용
            query = self.db.collection('analysis_sessions')\
                          .where('project_id', '>=', keyword)\
                          .where('project_id', '<', keyword + '\uf8ff')\
                          .order_by('project_id')\
                          .limit(limit)
            
            docs = query.stream()
            
            sessions = []
            for doc in docs:
                data = doc.to_dict()
                data['id'] = doc.id
                sessions.append(data)
            
            return sessions
            
        except Exception as e:
            logger.error(f"검색 중 오류: {e}")
            return []
        
    def _sanitize_data(self, data: Dict) -> Dict:
        """Firestore 저장을 위해 데이터를 안전하게 변환"""
        sanitized = {}
        
        for key, value in data.items():
            if value is None:
                sanitized[key] = ""
            elif isinstance(value, (int, float)):
                sanitized[key] = str(value)
            elif isinstance(value, list):
                # 리스트의 모든 요소를 문자열로 변환
                sanitized[key] = [str(item) for item in value]
            elif isinstance(value, dict):
                # 중첩 딕셔너리도 재귀적으로 변환
                sanitized[key] = self._sanitize_data(value)
            else:
                sanitized[key] = str(value)
        
        return sanitized

# 글로벌 데이터베이스 매니저 인스턴스
db_manager = FirestoreManager()