# mcp_server/ontology_engine/memory/cold.py
"""
JsonlCold — Phase 1 cold tier (append-only JSONL, lifetime 보관)

특징:
- 월 단위 파일 로테이션 (cold/2026-04.jsonl)
- append-only (쓰기 빠름, 조회는 선형 스캔)
- 삭제는 tombstone 방식 (물리적 삭제 X)

Phase 2 교체 후보: S3ArchiveCold / BigQueryCold (장기 보관 + 분석)
"""
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, List, Dict

from .base import MemoryTier


class JsonlCold(MemoryTier):
    def __init__(self, base_dir: str = "./data/memory/cold/"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _current_file(self) -> Path:
        """현재 월 기준 파일 (로테이션)"""
        month = datetime.utcnow().strftime("%Y-%m")
        return self.base_dir / f"{month}.jsonl"

    def put(self, key: str, value: Any, ttl_sec: Optional[int] = None) -> None:
        """append — 이전 값이 있어도 덮어쓰지 않고 새 줄 추가 (히스토리 보존)"""
        record = {
            "key": key,
            "value": value,
            "stored_at": time.time(),
            "ttl_sec": ttl_sec,  # cold 는 보통 None (lifetime)
            "deleted": False,
        }
        with open(self._current_file(), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def get(self, key: str) -> Optional[Any]:
        """모든 파일 역순 스캔 — 가장 최근 (+ 삭제 안된) 값 반환"""
        files = sorted(self.base_dir.glob("*.jsonl"), reverse=True)
        for fp in files:
            try:
                with open(fp, encoding="utf-8") as f:
                    lines = f.readlines()
                for line in reversed(lines):
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("key") == key:
                        if rec.get("deleted"):
                            return None
                        return rec.get("value")
            except Exception:
                continue
        return None

    def delete(self, key: str) -> bool:
        """tombstone append — 물리 삭제 안 함"""
        if self.get(key) is None:
            return False
        record = {"key": key, "deleted": True, "stored_at": time.time()}
        with open(self._current_file(), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True

    def size(self) -> int:
        """전체 라인 수 (tombstone 포함 — 근사값)"""
        total = 0
        for fp in self.base_dir.glob("*.jsonl"):
            try:
                with open(fp, encoding="utf-8") as f:
                    total += sum(1 for _ in f)
            except Exception:
                pass
        return total

    def list_keys(self, limit: int = 100) -> List[str]:
        """최근 파일부터 고유 키 수집"""
        seen: List[str] = []
        files = sorted(self.base_dir.glob("*.jsonl"), reverse=True)
        for fp in files:
            try:
                with open(fp, encoding="utf-8") as f:
                    for line in reversed(f.readlines()):
                        try:
                            rec = json.loads(line)
                            k = rec.get("key")
                            if k and k not in seen:
                                seen.append(k)
                                if len(seen) >= limit:
                                    return seen
                        except json.JSONDecodeError:
                            continue
            except Exception:
                continue
        return seen

    def sweep_expired(self) -> int:
        """cold 는 lifetime 보관 — TTL 청소 없음"""
        return 0

    def stats(self) -> Dict[str, Any]:
        base = super().stats()
        base.update({
            "base_dir": str(self.base_dir),
            "current_file": str(self._current_file().name),
            "file_count": len(list(self.base_dir.glob("*.jsonl"))),
        })
        return base
