# mcp_server/agents/helpdesk_agent.py
"""
Helpdesk Agent: 내부 직원용 헬프데스크 전담
- VectorDB(internal_docs Collection)에서 내부 문서 검색
- IT 정책, HR 규정, Finance 절차 등 내부 문서 기반 답변
- 내부 직원 대상 (전 부서용)
"""
import sys
import asyncio
from .base_agent import BaseAgent


class HelpdeskAgent(BaseAgent):
    """내부 헬프데스크 전문 Agent"""

    def __init__(self, llm_config: dict, service_manager=None):
        super().__init__(
            name="Helpdesk Agent",
            description="내부 직원용 헬프데스크를 전담합니다. "
                       "IT 정책(VPN, 계정), HR 규정(연차, 복지), Finance 절차(경비, 예산) 등 "
                       "내부 문서를 기반으로 직원 문의에 답변합니다. "
                       "VectorDB의 internal_docs 컬렉션에서 내부 문서를 검색합니다.",
            llm_config=llm_config,
        )
        self.service_manager = service_manager

    def register_tools_from_services(self, user_id: str = None):
        """내부 문서 RAG 도구 등록"""
        from ..services import vectordb_service, openai_service

        # 내부 문서 업로드 (internal_docs Collection)
        async def upload_internal_document(content: str, file_name: str = None,
                                            department: str = "general", **kwargs):
            """내부 문서를 VectorDB에 업로드합니다 (content, file_name, department)"""
            # OpenAI가 filename, doc_name 등 다른 이름으로 보낼 수 있음
            if not file_name:
                file_name = kwargs.get('filename') or kwargs.get('doc_name') or kwargs.get('name') or 'untitled.txt'
            try:
                chunks = vectordb_service.split_text_into_chunks(content)
                embeddings_list = []
                for chunk in chunks:
                    emb = await asyncio.to_thread(openai_service.create_embedding, chunk)
                    if emb:
                        embeddings_list.append(emb)

                if not embeddings_list:
                    return {'success': False, 'error': '임베딩 생성 실패'}

                result = vectordb_service.add_document(
                    content=content,
                    file_name=file_name,
                    embeddings=embeddings_list,
                    collection_name='internal_docs',
                    metadata_extra={'department': department},
                )
                return {
                    'success': True,
                    'file_name': file_name,
                    'department': department,
                    'chunks': len(chunks),
                }
            except Exception as e:
                return {'success': False, 'error': str(e)}

        # 내부 문서 검색
        async def search_internal_documents(query: str, department: str = None,
                                             top_k: int = 5):
            """내부 문서를 검색합니다 (부서별 필터 가능)"""
            query_embedding = await asyncio.to_thread(openai_service.create_embedding, query)
            if not query_embedding:
                return {'success': False, 'error': '임베딩 생성 실패'}

            where_filter = None
            if department:
                where_filter = {'department': department}

            results = vectordb_service.search_documents(
                query_embedding, top_k=top_k,
                collection_name='internal_docs',
                where_filter=where_filter,
            )
            return {'success': True, 'results': results}

        # 내부 문의 응대 (RAG)
        async def ask_helpdesk(question: str, department: str = None):
            """직원 문의에 대해 내부 문서 기반으로 답변합니다"""
            query_embedding = await asyncio.to_thread(openai_service.create_embedding, question)
            if not query_embedding:
                return {'success': False, 'error': '임베딩 생성 실패'}

            where_filter = None
            if department:
                where_filter = {'department': department}

            results = vectordb_service.search_documents(
                query_embedding, top_k=3,
                collection_name='internal_docs',
                where_filter=where_filter,
            )

            if not results:
                return {
                    'success': True,
                    'answer': '관련 내부 문서를 찾을 수 없습니다. 해당 부서에 직접 문의해주세요.',
                    'sources': [],
                }

            context_parts = []
            sources = []
            departments = []
            for r in results:
                context_parts.append(r.get('content', ''))
                sources.append(r.get('file_name', 'unknown'))
                departments.append(r.get('department', 'general'))

            context = "\n\n---\n\n".join(context_parts)

            answer = await asyncio.to_thread(
                openai_service.generate_text_with_system,
                system_prompt="""당신은 회사 내부 헬프데스크 전문가입니다.
회사 내부 문서(IT 정책, HR 규정, Finance 절차 등)를 기반으로 직원의 질문에 답변하세요.

규칙:
1. 내부 문서에 있는 정보만 사용하세요
2. 문서에 없는 내용은 "해당 부서에 직접 확인해주세요"라고 안내하세요
3. 절차가 있는 경우 단계별로 명확히 안내하세요
4. 담당 부서/담당자 정보가 있으면 함께 안내하세요
5. 한국어로 답변하세요""",
                user_prompt=f"내부 문서 내용:\n{context}\n\n직원 질문: {question}",
                temperature=0.3,
                max_tokens=1000,
            )

            return {
                'success': True,
                'answer': answer,
                'sources': list(set(sources)),
                'departments': list(set(departments)),
            }

        # 내부 문서 목록
        async def list_internal_documents(department: str = None):
            """업로드된 내부 문서 목록을 조회합니다"""
            return vectordb_service.list_documents(
                collection_name='internal_docs',
                department=department,
            )

        # 내부 문서 삭제
        async def delete_internal_document(file_name: str):
            """내부 문서를 삭제합니다"""
            return vectordb_service.delete_document(
                file_name=file_name,
                collection_name='internal_docs',
            )

        # 도구 등록
        self.register_tool('upload_internal_document', upload_internal_document,
                          '내부 문서를 업로드합니다 (content, file_name, department: IT/HR/Finance)')
        self.register_tool('search_internal_documents', search_internal_documents,
                          '내부 문서를 검색합니다 (query, department, top_k)')
        self.register_tool('ask_helpdesk', ask_helpdesk,
                          '직원 문의에 내부 문서 기반으로 답변합니다 (question, department)')
        self.register_tool('list_internal_documents', list_internal_documents,
                          '업로드된 내부 문서 목록을 조회합니다')
        self.register_tool('delete_internal_document', delete_internal_document,
                          '내부 문서를 삭제합니다 (file_name)')

        print(f"[Helpdesk Agent] {len(self._tools)} tools registered for user: {user_id}", file=sys.stderr)
