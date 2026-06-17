# mcp_server/ontology_engine/adapters/salesforce.py
"""
Salesforce SourceAdapter — SOQL 기반 단건/일괄 조회

기존 mcp_server/services/salesforce_service.py 의 access_token / instance_url 재활용.
별도 인증 로직 추가하지 않음.

Phase 1 구현:
- fetch_one: SOQL LIKE WHERE LIMIT 1
- fetch_batch: SOQL IN (...) 으로 묶어서 한 번에
- health_check: /services/data 엔드포인트 ping

Phase 2 확장 여지 (지금은 미구현):
- 캐시 레이어 (이 어댑터 위에 LRU)
- 재시도/circuit breaker (retry 라이브러리)
- 비동기 (async client)
"""
import logging
import re
from typing import List, Optional, Dict, Any

import requests

from .base import SourceAdapter

logger = logging.getLogger(__name__)


class SalesforceAdapter(SourceAdapter):
    """Salesforce REST API (SOQL) 기반 어댑터"""

    def _get_session(self):
        """
        SFDC 세션 획득 (멀티유저 모드 우선).

        우선순위:
          1. service_manager.get_current_user() 가 반환하는 user_id 의 per-user 세션
          2. _user_sf_sessions 의 첫 키 (단일 admin 환경 폴백)
          3. 글로벌 _access_token (legacy 단일 사용자 모드 호환)

        OOSDK 멀티유저 환경에선 (1) 또는 (2) 가 hit 되어야 정상.
        (3) 까지 떨어지면 None 반환되고 customer:null 됨 → 그 경우 호출자가 trace 에 표시.
        """
        from mcp_server.services import salesforce_service
        from mcp_server.services.service_manager import get_current_user

        # 1) 현재 요청 컨텍스트의 user_id
        user_id = None
        try:
            user_id = get_current_user()
        except Exception:
            pass

        access_token, instance_url = salesforce_service._get_sf_session(user_id)

        # 2) 폴백: per-user 세션 dict 의 첫 키 (admin 1명만 있는 데모 환경 안전망)
        if not access_token:
            try:
                sessions = getattr(salesforce_service, "_user_sf_sessions", None) or {}
                if sessions:
                    first_user = next(iter(sessions.keys()))
                    fallback = sessions[first_user]
                    access_token = fallback.get("access_token")
                    instance_url = fallback.get("instance_url")
                    logger.info(f"[SFDC] _get_session 폴백 사용: user_id={first_user}")
            except Exception as e:
                logger.warning(f"[SFDC] _user_sf_sessions 폴백 실패: {e}")

        return access_token, instance_url

    # ---------------------------------------------------------------
    # fetch_one
    # ---------------------------------------------------------------
    def fetch_one(self, lookup_value: str) -> Optional[Dict[str, Any]]:
        access_token, instance_url = self._get_session()
        if not access_token or not instance_url:
            logger.warning("[SFDC] 세션 없음 — 인증 안 됨. None 반환.")
            # 호출자가 진단할 수 있도록 마지막 에러를 인스턴스에 노출
            self.last_error = "sfdc_session_unavailable"
            return None
        # 정상 경로 진입 — 이전 에러 클리어
        self.last_error = None

        # SOQL 템플릿 치환
        # email / email_domain 둘 다 지원 (yaml 의 lookup.by 값에 따라)
        query_template = self.config.get("lookup", {}).get("query", "")
        scope = self.config.get("scope", "")
        # email 은 단순 escape (영숫자 외 허용 — @ . _ - 등 필요)
        safe_email = self._escape_email(lookup_value)
        # email_domain 은 도메인 한정 sanitize (기존 동작 유지)
        safe_domain = self._escape_soql(lookup_value)
        try:
            query = query_template.format(
                email=safe_email,
                email_domain=safe_domain,
                scope=scope or "true",
            )
        except KeyError as e:
            logger.error(f"[SFDC] query 템플릿 변수 누락: {e}")
            return None

        # 실행
        records = self._execute_query(query, access_token, instance_url)
        if not records:
            # 쿼리 자체가 0건 — 인증은 됐지만 매칭 레코드 없음 (정상)
            self.last_error = "no_match"
            return None

        # 필드 매핑 적용
        self.last_error = None
        return self._apply_field_map(records[0])

    # ---------------------------------------------------------------
    # fetch_batch
    # ---------------------------------------------------------------
    def fetch_batch(self, lookup_values: List[str]) -> List[Optional[Dict[str, Any]]]:
        """
        IN 절로 묶어서 한 번에 조회. 결과를 lookup_value 별로 매핑.
        주의: SFDC SOQL IN 은 200건 제한. 그 이상이면 chunking 필요 (Phase 2).
        """
        if not lookup_values:
            return []

        access_token, instance_url = self._get_session()
        if not access_token or not instance_url:
            return [None] * len(lookup_values)

        # batch_lookup 설정 없으면 fetch_one 반복으로 폴백
        batch_cfg = self.config.get("batch_lookup")
        if not batch_cfg:
            return [self.fetch_one(v) for v in lookup_values]

        # IN 절 구성 (단순 문자열 join — escape 주의)
        escaped = [f"'%{self._escape_soql(v)}%'" for v in lookup_values]
        in_clause = ", ".join(escaped)
        query = batch_cfg["query"].format(domains=in_clause)

        records = self._execute_query(query, access_token, instance_url)

        # lookup_value 별로 매핑 (도메인 매칭이 fuzzy 라 정확 매칭은 어댑터 책임)
        results: List[Optional[Dict[str, Any]]] = []
        domain_field = self.config.get("field_map", {}).get("email_domain", "Website")
        for v in lookup_values:
            matched = next(
                (r for r in records if v.lower() in str(r.get(domain_field, "")).lower()),
                None,
            )
            results.append(self._apply_field_map(matched) if matched else None)
        return results

    # ---------------------------------------------------------------
    # health_check
    # ---------------------------------------------------------------
    def health_check(self) -> bool:
        access_token, instance_url = self._get_session()
        if not access_token or not instance_url:
            return False
        try:
            api_version = self._get_api_version()
            url = f"{instance_url}/services/data/{api_version}/limits/"
            r = requests.get(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=5,
            )
            return r.status_code == 200
        except Exception as e:
            logger.warning(f"[SFDC] health_check 실패: {e}")
            return False

    # ---------------------------------------------------------------
    # internals
    # ---------------------------------------------------------------
    def _execute_query(self, soql: str, access_token: str, instance_url: str) -> List[Dict]:
        """SOQL 실행 → records 리스트"""
        api_version = self._get_api_version()
        url = f"{instance_url}/services/data/{api_version}/query/"

        try:
            r = requests.get(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                params={"q": soql},
                timeout=30,
            )
            if r.status_code != 200:
                logger.error(f"[SFDC] query 실패 status={r.status_code} body={r.text[:200]}")
                self.last_error = f"http_{r.status_code}: {r.text[:120]}"
                return []
            return r.json().get("records", [])
        except Exception as e:
            logger.error(f"[SFDC] query 예외: {e}")
            self.last_error = f"exception: {e}"
            return []

    def _get_api_version(self) -> str:
        """connection 의 api_version 또는 기본값"""
        conn_name = self.config.get("connection")
        if conn_name and conn_name in self.connections:
            return self.connections[conn_name].get("api_version", "v60.0")
        return "v60.0"

    @staticmethod
    def _escape_soql(value: str) -> str:
        """SOQL injection 방지를 위한 단순 이스케이프 (도메인용)"""
        if not isinstance(value, str):
            return ""
        # SOQL 특수문자: ' \\ %
        value = value.replace("\\", "\\\\").replace("'", "\\'")
        # 기본 sanitize: 알파벳/숫자/도트/하이픈만 (도메인 케이스에 적합)
        value = re.sub(r"[^a-zA-Z0-9._\-]", "", value)
        return value

    @staticmethod
    def _escape_email(value: str) -> str:
        """이메일용 sanitize (@ + . _ - 허용)"""
        if not isinstance(value, str):
            return ""
        value = value.replace("\\", "\\\\").replace("'", "\\'")
        # 'John <john@x.com>' 형태 처리
        m = re.search(r"<([^>]+)>", value)
        if m:
            value = m.group(1)
        # 이메일에 안전한 문자만
        value = re.sub(r"[^a-zA-Z0-9@._\-+]", "", value)
        return value.lower()
