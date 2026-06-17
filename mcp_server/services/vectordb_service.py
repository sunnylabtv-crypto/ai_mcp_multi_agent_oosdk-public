# mcp_server/services/vectordb_service.py
"""
ChromaDB 기반 Vector Database 서비스 (Multi-Collection)
- product_docs: CS Agent용 (제품 FAQ, 매뉴얼, 반품규정)
- internal_docs: Helpdesk Agent용 (IT/HR/Finance 정책문서)
- it_helpdesk_docs: 기존 호환용 (하위 호환)
"""

import os
import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

import chromadb
from chromadb.config import Settings

logger = logging.getLogger(__name__)

# 전역 변수
_chroma_client = None
_collections = {}  # {collection_name: collection_instance}

# 설정
CHROMA_PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", "/app/data/multi/chromadb")

# 지원하는 컬렉션 정의
COLLECTIONS = {
    'product_docs': {
        'description': 'Customer Service Knowledge Base (제품 FAQ, 매뉴얼, 반품규정)',
        'agent': 'CS Agent',
    },
    'internal_docs': {
        'description': 'Internal Helpdesk Knowledge Base (IT/HR/Finance 정책문서)',
        'agent': 'Helpdesk Agent',
    },
    'it_helpdesk_docs': {
        'description': 'Legacy IT Helpdesk (하위 호환)',
        'agent': 'Legacy',
    },
}


def initialize(config: dict = None) -> bool:
    """ChromaDB 초기화 (모든 컬렉션)"""
    global _chroma_client, _collections

    try:
        persist_dir = config.get('CHROMA_PERSIST_DIR', CHROMA_PERSIST_DIR) if config else CHROMA_PERSIST_DIR
        os.makedirs(persist_dir, exist_ok=True)

        _chroma_client = chromadb.PersistentClient(
            path=persist_dir,
            settings=Settings(anonymized_telemetry=False)
        )

        # 모든 컬렉션 생성/가져오기
        for name, meta in COLLECTIONS.items():
            _collections[name] = _chroma_client.get_or_create_collection(
                name=name,
                metadata={"description": meta['description']}
            )
            count = _collections[name].count()
            logger.info(f"  📚 Collection '{name}': {count}개 청크")

        logger.info(f"✅ ChromaDB 초기화 완료 ({len(_collections)}개 컬렉션, 경로: {persist_dir})")
        return True

    except Exception as e:
        logger.error(f"❌ ChromaDB 초기화 실패: {e}")
        return False


def _get_collection(collection_name: str = None):
    """컬렉션 인스턴스 반환 (기본: it_helpdesk_docs)"""
    if collection_name is None:
        collection_name = 'it_helpdesk_docs'

    if collection_name in _collections:
        return _collections[collection_name]

    # 동적으로 컬렉션 생성
    if _chroma_client:
        _collections[collection_name] = _chroma_client.get_or_create_collection(
            name=collection_name
        )
        return _collections[collection_name]

    return None


def get_status() -> dict:
    """VectorDB 상태 확인"""
    if _chroma_client is None:
        return {"initialized": False, "chunk_count": 0}

    collection_stats = {}
    total_chunks = 0
    for name, coll in _collections.items():
        count = coll.count()
        collection_stats[name] = {
            'chunk_count': count,
            'description': COLLECTIONS.get(name, {}).get('description', ''),
        }
        total_chunks += count

    return {
        "initialized": True,
        "total_chunks": total_chunks,
        "persist_dir": CHROMA_PERSIST_DIR,
        "collections": collection_stats,
    }


def generate_doc_id(file_name: str, chunk_id: int, collection_name: str = '') -> str:
    """문서 ID 생성"""
    return hashlib.md5(f"{collection_name}_{file_name}_{chunk_id}".encode()).hexdigest()


def split_text_into_chunks(text: str, chunk_size: int = 1000, overlap: int = 200) -> List[str]:
    """텍스트를 청크로 분할"""
    chunks = []
    start = 0

    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]

        if end < len(text):
            last_period = chunk.rfind('.')
            last_newline = chunk.rfind('\n')
            cut_point = max(last_period, last_newline)
            if cut_point > chunk_size // 2:
                chunk = text[start:start + cut_point + 1]
                end = start + cut_point + 1

        if chunk.strip():
            chunks.append(chunk.strip())
        start = end - overlap

    return chunks


def add_document(
    content: str,
    file_name: str,
    embeddings: List[List[float]],
    file_path: str = "direct_upload",
    collection_name: str = None,
    metadata_extra: dict = None,
) -> Dict[str, Any]:
    """
    문서를 Vector DB에 추가

    Args:
        content: 문서 내용
        file_name: 파일명
        embeddings: 청크별 임베딩 벡터 리스트
        file_path: 파일 경로
        collection_name: 컬렉션 이름 (None이면 기본 it_helpdesk_docs)
        metadata_extra: 추가 메타데이터 (예: {'department': 'IT'})
    """
    collection = _get_collection(collection_name)
    if collection is None:
        return {"status": "error", "message": "VectorDB가 초기화되지 않았습니다."}

    try:
        chunks = split_text_into_chunks(content)

        if len(chunks) != len(embeddings):
            return {
                "status": "error",
                "message": f"청크 수({len(chunks)})와 임베딩 수({len(embeddings)})가 일치하지 않습니다."
            }

        ids = []
        documents = []
        metadatas = []
        coll_name = collection_name or 'it_helpdesk_docs'

        for i, chunk in enumerate(chunks):
            doc_id = generate_doc_id(file_name, i, coll_name)
            ids.append(doc_id)
            documents.append(chunk)

            meta = {
                "file_name": file_name,
                "file_path": file_path,
                "chunk_id": i,
                "total_chunks": len(chunks),
                "collection": coll_name,
                "uploaded_at": datetime.now().isoformat(),
            }
            if metadata_extra:
                meta.update(metadata_extra)
            metadatas.append(meta)

        collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
        )

        logger.info(f"✅ 문서 추가: {file_name} → {coll_name} ({len(chunks)}개 청크)")

        return {
            "status": "success",
            "file_name": file_name,
            "collection": coll_name,
            "chunks_created": len(chunks),
        }

    except Exception as e:
        logger.error(f"❌ 문서 추가 실패: {e}")
        return {"status": "error", "message": str(e)}


def search_documents(
    query_embedding: List[float],
    top_k: int = 5,
    collection_name: str = None,
    where_filter: dict = None,
) -> List[Dict[str, Any]]:
    """
    유사 문서 검색

    Args:
        query_embedding: 쿼리 임베딩 벡터
        top_k: 반환할 결과 수
        collection_name: 컬렉션 이름
        where_filter: 메타데이터 필터 (예: {'department': 'IT'})
    """
    collection = _get_collection(collection_name)
    if collection is None:
        return []

    try:
        if collection.count() == 0:
            return []

        query_params = {
            'query_embeddings': [query_embedding],
            'n_results': min(top_k, collection.count()),
        }
        if where_filter:
            query_params['where'] = where_filter

        results = collection.query(**query_params)

        search_results = []
        if results['documents'] and results['documents'][0]:
            for i, doc in enumerate(results['documents'][0]):
                metadata = results['metadatas'][0][i] if results['metadatas'] else {}
                distance = results['distances'][0][i] if results['distances'] else 0
                similarity = max(0, 1 - (distance / 2))

                search_results.append({
                    "content": doc,
                    "file_name": metadata.get("file_name", "Unknown"),
                    "similarity": round(similarity, 3),
                    "chunk_id": metadata.get("chunk_id", 0),
                    "department": metadata.get("department", ""),
                    "metadata": metadata,
                })

        return search_results

    except Exception as e:
        logger.error(f"❌ 검색 실패: {e}")
        return []


def list_documents(collection_name: str = None, department: str = None) -> Dict[str, Any]:
    """업로드된 문서 목록 조회"""
    collection = _get_collection(collection_name)
    if collection is None:
        return {"total_documents": 0, "total_chunks": 0, "documents": []}

    try:
        if department:
            all_data = collection.get(where={"department": department})
        else:
            all_data = collection.get()

        if not all_data['metadatas']:
            return {"total_documents": 0, "total_chunks": 0, "documents": []}

        file_stats = {}
        for metadata in all_data['metadatas']:
            file_name = metadata.get('file_name', 'Unknown')
            if file_name not in file_stats:
                file_stats[file_name] = {
                    "file_name": file_name,
                    "chunks": 0,
                    "department": metadata.get('department', ''),
                    "uploaded_at": metadata.get('uploaded_at', 'Unknown'),
                }
            file_stats[file_name]["chunks"] += 1

        return {
            "collection": collection_name or 'it_helpdesk_docs',
            "total_documents": len(file_stats),
            "total_chunks": len(all_data['metadatas']),
            "documents": list(file_stats.values()),
        }

    except Exception as e:
        logger.error(f"❌ 문서 목록 조회 실패: {e}")
        return {"total_documents": 0, "total_chunks": 0, "documents": [], "error": str(e)}


def delete_document(file_name: str, collection_name: str = None) -> Dict[str, Any]:
    """특정 문서 삭제"""
    collection = _get_collection(collection_name)
    if collection is None:
        return {"status": "error", "message": "VectorDB가 초기화되지 않았습니다."}

    try:
        all_data = collection.get()
        ids_to_delete = []

        for i, metadata in enumerate(all_data['metadatas']):
            if metadata.get('file_name') == file_name:
                ids_to_delete.append(all_data['ids'][i])

        if not ids_to_delete:
            return {"status": "warning", "message": f"'{file_name}' 문서를 찾을 수 없습니다."}

        collection.delete(ids=ids_to_delete)
        logger.info(f"✅ 문서 삭제: {file_name} ({len(ids_to_delete)}개 청크)")

        return {
            "status": "success",
            "file_name": file_name,
            "collection": collection_name or 'it_helpdesk_docs',
            "chunks_deleted": len(ids_to_delete),
        }

    except Exception as e:
        logger.error(f"❌ 문서 삭제 실패: {e}")
        return {"status": "error", "message": str(e)}
