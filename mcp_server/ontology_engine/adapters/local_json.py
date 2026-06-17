# mcp_server/ontology_engine/adapters/local_json.py
"""
LocalJsonAdapter — 로컬 JSON 파일 기반 어댑터

용도:
- SFDC 장애 시 fallback
- 로컬 개발 / 단위 테스트
- 촬영 시 네트워크 의존성 제거

파일 포맷: object_types.<Type>.source.path 가 가리키는 json 배열
예: [{"id": "...", "email_domain": "acme-corp.com", "tier": "VIP", ...}, ...]
"""
import json
import logging
from pathlib import Path
from typing import List, Optional, Dict, Any

from .base import SourceAdapter

logger = logging.getLogger(__name__)


class LocalJsonAdapter(SourceAdapter):
    """JSON 파일에서 읽는 어댑터"""

    def __init__(self, source_config: Dict[str, Any], connections: Optional[Dict] = None):
        super().__init__(source_config, connections)
        self._cache: Optional[List[Dict]] = None
        self._path = Path(source_config.get("path", ""))

    def _load(self) -> List[Dict]:
        if self._cache is not None:
            return self._cache
        if not self._path.exists():
            logger.warning(f"[LocalJson] 파일 없음: {self._path}")
            self._cache = []
            return self._cache
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                logger.error(f"[LocalJson] 배열 형식이 아님: {self._path}")
                self._cache = []
                return self._cache
            self._cache = data
            return self._cache
        except Exception as e:
            logger.error(f"[LocalJson] 읽기 실패: {e}")
            self._cache = []
            return self._cache

    def fetch_one(self, lookup_value: str) -> Optional[Dict[str, Any]]:
        records = self._load()
        lookup_by = self.config.get("lookup", {}).get("by", "email_domain")
        for r in records:
            if r.get(lookup_by) == lookup_value:
                return r
        return None

    def fetch_batch(self, lookup_values: List[str]) -> List[Optional[Dict[str, Any]]]:
        records = self._load()
        lookup_by = self.config.get("lookup", {}).get("by", "email_domain")
        # 1 pass 로 dict 만들기
        indexed = {r.get(lookup_by): r for r in records}
        return [indexed.get(v) for v in lookup_values]

    def health_check(self) -> bool:
        return self._path.exists()
