# mcp_server/tools/company_helpdesk_tools.py
"""
Company Helpdesk MCP 도구들
Claude Desktop이 호출할 수 있는 회사 문서 관련 함수들
- RAG 기반 문서 검색 (IT, HR, Finance 등 모든 회사 문서)
- ChromaDB Vector Store
- OpenAI 임베딩 및 답변 생성
"""
import logging
from typing import Dict, List

logger = logging.getLogger(__name__)

# 서비스 함수들을 import
from ..services import vectordb_service, openai_service


def register_company_helpdesk_tools(mcp):
    """Company Helpdesk 도구들을 MCP 서버에 등록"""
    
    @mcp.tool()
    def upload_company_document(content: str, file_name: str) -> Dict:
        """
        회사 문서를 Vector DB에 업로드합니다.
        IT, HR, Finance 등 모든 종류의 회사 가이드 문서를 저장할 수 있습니다.
        
        Args:
            content: 문서 내용 (텍스트)
            file_name: 문서 식별을 위한 파일명 (예: "VPN_설정_가이드.txt", "연차신청_안내.txt")
            
        Returns:
            업로드 결과 정보
            
        Example:
            upload_company_document(
                content="VPN 연결 방법: 1. 먼저 VPN 클라이언트를 설치합니다...",
                file_name="VPN_설정_가이드.txt"
            )
        """
        logger.info(f"📄 회사 문서 업로드 요청: {file_name}")
        
        try:
            if not content or len(content.strip()) < 10:
                return {
                    "status": "error",
                    "message": "문서 내용이 너무 짧습니다. (최소 10자 이상)"
                }
            
            # 청크 분할
            chunks = vectordb_service.split_text_into_chunks(content)
            
            if not chunks:
                return {
                    "status": "error",
                    "message": "문서에서 유효한 텍스트를 추출할 수 없습니다."
                }
            
            # 각 청크에 대해 임베딩 생성
            logger.info(f"   임베딩 생성 중... ({len(chunks)}개 청크)")
            embeddings = []
            for chunk in chunks:
                embedding = openai_service.create_embedding(chunk)
                if embedding:
                    embeddings.append(embedding)
                else:
                    return {
                        "status": "error",
                        "message": "임베딩 생성에 실패했습니다."
                    }
            
            # Vector DB에 저장
            result = vectordb_service.add_document(
                content=content,
                file_name=file_name,
                embeddings=embeddings
            )
            
            if result["status"] == "success":
                logger.info(f"✅ 회사 문서 업로드 완료: {file_name}")
                return {
                    "status": "success",
                    "file_name": file_name,
                    "chunks_created": result["chunks_created"],
                    "message": f"'{file_name}' 문서가 성공적으로 업로드되었습니다. ({result['chunks_created']}개 청크)"
                }
            else:
                return result
                
        except Exception as e:
            logger.error(f"❌ 회사 문서 업로드 실패: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"업로드 실패: {str(e)}"
            }
    

    @mcp.tool()
    def search_company_documents(query: str, top_k: int = 5) -> Dict:
        """
        회사 문서에서 관련 내용을 검색합니다.
        IT, HR, Finance 등 모든 회사 문서에서 검색합니다.
        
        Args:
            query: 검색 쿼리 (예: "VPN 연결 오류", "연차 신청 방법", "경비 청구")
            top_k: 반환할 결과 수 (기본값: 5)
            
        Returns:
            검색 결과 목록
            
        Example:
            search_company_documents(query="이메일 설정 방법", top_k=3)
        """
        logger.info(f"🔍 회사 문서 검색: {query}")
        
        try:
            # VectorDB 상태 확인
            status = vectordb_service.get_status()
            if not status["initialized"]:
                return {
                    "status": "error",
                    "message": "VectorDB가 초기화되지 않았습니다."
                }
            
            if status["chunk_count"] == 0:
                return {
                    "status": "warning",
                    "message": "등록된 회사 문서가 없습니다. 먼저 문서를 업로드해주세요.",
                    "results": []
                }
            
            # 쿼리 임베딩 생성
            query_embedding = openai_service.create_embedding(query)
            if not query_embedding:
                return {
                    "status": "error",
                    "message": "검색 쿼리 임베딩 생성에 실패했습니다.",
                    "results": []
                }
            
            # 검색 실행
            results = vectordb_service.search_documents(query_embedding, top_k)
            
            # 결과 정리
            formatted_results = []
            for r in results:
                formatted_results.append({
                    "content": r["content"][:500] + "..." if len(r["content"]) > 500 else r["content"],
                    "full_content": r["content"],
                    "file_name": r["file_name"],
                    "similarity": r["similarity"]
                })
            
            logger.info(f"   검색 결과: {len(formatted_results)}개")
            
            return {
                "status": "success",
                "query": query,
                "total_results": len(formatted_results),
                "results": formatted_results
            }
            
        except Exception as e:
            logger.error(f"❌ 회사 문서 검색 실패: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"검색 실패: {str(e)}",
                "results": []
            }
    

    @mcp.tool()
    def ask_company_helpdesk(question: str) -> Dict:
        """
        회사 관련 질문에 대해 문서 기반으로 답변합니다.
        IT, HR, Finance 등 모든 회사 문서를 RAG 방식으로 검색하여 AI가 답변을 생성합니다.
        
        Args:
            question: 회사 관련 질문 (예: "VPN 연결이 안 돼요", "연차 신청 방법", "경비 청구 어떻게 해요?")
            
        Returns:
            AI 답변 및 참고 문서 정보
            
        Example:
            ask_company_helpdesk(question="프린터 드라이버 설치는 어떻게 하나요?")
        """
        logger.info(f"💬 회사 Helpdesk 질문: {question}")
        
        try:
            # 1. 관련 문서 검색
            search_result = search_company_documents(question, top_k=3)
            
            # 2. 컨텍스트 구성
            context = ""
            references = []
            
            if search_result["status"] == "success" and search_result.get("results"):
                for i, result in enumerate(search_result["results"]):
                    if result["similarity"] > 0.3:  # 유사도 임계값
                        context += f"\n[참고문서 {i+1}] {result['file_name']}:\n"
                        context += f"{result['full_content']}\n"
                        references.append({
                            "file_name": result["file_name"],
                            "similarity": result["similarity"]
                        })
            
            # 3. 시스템 프롬프트
            system_prompt = """너는 전문적이고 친근한 회사 Helpdesk 담당자야.
IT, HR, Finance 등 회사 업무 관련 문제를 해결하고 질문에 답변하는 것이 주된 역할이야.

답변할 때 다음 사항을 지켜줘:
1. 제공된 문서 내용을 우선적으로 참고하여 정확한 정보 제공
2. 기술적 용어는 쉽게 설명하되 전문성 유지
3. 단계별로 구체적인 해결 방법 제시
4. 문서에 없는 내용은 "등록된 문서에서 관련 정보를 찾지 못했습니다"라고 안내
5. 항상 예의바르고 도움이 되는 톤으로 응답
6. 한국어로 답변

문서에서 관련 정보를 찾았다면 그것을 바탕으로 답변하고,
찾지 못했다면 문서에 없다고 명확히 안내해줘."""

            # 4. 프롬프트 구성
            if context:
                user_message = f"{context}\n\n사용자 질문: {question}"
            else:
                user_message = f"사용자 질문: {question}\n\n(참고: 등록된 문서에서 관련 내용을 찾지 못했습니다)"
            
            # 5. GPT 호출
            answer = openai_service.generate_text_with_system(
                system_prompt=system_prompt,
                user_prompt=user_message,
                temperature=0.7,
                max_tokens=1500
            )
            
            if not answer:
                return {
                    "status": "error",
                    "message": "답변 생성에 실패했습니다."
                }
            
            logger.info(f"✅ 회사 Helpdesk 답변 생성 완료 (참고 문서: {len(references)}개)")
            
            return {
                "status": "success",
                "question": question,
                "answer": answer,
                "references": references,
                "documents_used": len(references) > 0
            }
            
        except Exception as e:
            logger.error(f"❌ 회사 Helpdesk 답변 생성 실패: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"답변 생성 실패: {str(e)}"
            }
    

    @mcp.tool()
    def list_company_documents() -> Dict:
        """
        업로드된 회사 문서 목록을 조회합니다.
        
        Returns:
            문서 목록 및 통계 정보
            
        Example:
            list_company_documents()
        """
        logger.info("📋 회사 문서 목록 조회")
        
        try:
            result = vectordb_service.list_documents()
            
            return {
                "status": "success",
                "total_documents": result["total_documents"],
                "total_chunks": result["total_chunks"],
                "documents": result["documents"],
                "message": f"총 {result['total_documents']}개 문서, {result['total_chunks']}개 청크"
            }
            
        except Exception as e:
            logger.error(f"❌ 문서 목록 조회 실패: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"조회 실패: {str(e)}"
            }
    

    @mcp.tool()
    def delete_company_document(file_name: str) -> Dict:
        """
        특정 회사 문서를 삭제합니다.
        
        Args:
            file_name: 삭제할 문서의 파일명
            
        Returns:
            삭제 결과
            
        Example:
            delete_company_document(file_name="구버전_VPN_가이드.txt")
        """
        logger.info(f"🗑️ 회사 문서 삭제 요청: {file_name}")
        
        try:
            result = vectordb_service.delete_document(file_name)
            
            if result["status"] == "success":
                logger.info(f"✅ 회사 문서 삭제 완료: {file_name}")
            
            return result
            
        except Exception as e:
            logger.error(f"❌ 문서 삭제 실패: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"삭제 실패: {str(e)}"
            }
    

    @mcp.tool()
    def get_company_helpdesk_status() -> Dict:
        """
        Company Helpdesk 시스템의 현재 상태를 확인합니다.
        
        Returns:
            시스템 상태 정보
            
        Example:
            get_company_helpdesk_status()
        """
        logger.info("📊 Company Helpdesk 상태 확인")
        
        try:
            vectordb_status = vectordb_service.get_status()
            openai_status = openai_service.get_service_status()
            
            return {
                "status": "success",
                "services": {
                    "vectordb": "✅ 연결됨" if vectordb_status["initialized"] else "❌ 미연결",
                    "openai": "✅ 연결됨" if openai_status["initialized"] else "❌ 미연결",
                    "document_count": vectordb_status.get("chunk_count", 0)
                },
                "config": {
                    "embedding_model": "text-embedding-3-small",
                    "persist_dir": vectordb_status.get("persist_dir", "N/A"),
                    "collection_name": vectordb_status.get("collection_name", "N/A")
                }
            }
            
        except Exception as e:
            logger.error(f"❌ 상태 확인 실패: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"상태 확인 실패: {str(e)}"
            }
    
    logger.info("✅ Company Helpdesk 도구 등록 완료 (6개 Tool)")
