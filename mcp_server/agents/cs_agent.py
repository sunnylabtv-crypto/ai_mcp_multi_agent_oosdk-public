# mcp_server/agents/cs_agent.py
"""
CS Agent: 고객 서비스 전담
- VectorDB(product_docs Collection)에서 제품 문서/FAQ/매뉴얼 검색
- 고객 문의 응대, 반품/교환 절차 안내
- 외부 고객 대상 (Sales/CS 부서용)
"""
import sys
import asyncio
from .base_agent import BaseAgent


class CSAgent(BaseAgent):
    """고객 서비스(CS) 전문 Agent"""

    def __init__(self, llm_config: dict, service_manager=None):
        super().__init__(
            name="CS Agent",
            description="고객 서비스를 전담합니다. 제품 FAQ, 반품/교환 절차, "
                       "제품 사용법, 고객 문의에 대한 답변을 제공합니다. "
                       "VectorDB의 product_docs 컬렉션에서 제품 관련 문서를 검색합니다.",
            llm_config=llm_config,
        )
        self.service_manager = service_manager

    def register_tools_from_services(self, user_id: str = None):
        """제품 문서 RAG 도구 등록"""
        from ..services import vectordb_service, openai_service

        # 제품 문서 업로드 (product_docs Collection)
        async def upload_product_document(content: str, file_name: str = None, **kwargs):
            """제품 관련 문서를 VectorDB에 업로드합니다 (content, file_name)"""
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
                    collection_name='product_docs',
                )
                return {'success': True, 'file_name': file_name, 'chunks': len(chunks)}
            except Exception as e:
                return {'success': False, 'error': str(e)}

        # 제품 문서 검색
        async def search_product_documents(query: str, top_k: int = 5):
            """제품 관련 문서를 검색합니다"""
            query_embedding = await asyncio.to_thread(openai_service.create_embedding, query)
            if not query_embedding:
                return {'success': False, 'error': '임베딩 생성 실패'}
            results = vectordb_service.search_documents(
                query_embedding, top_k=top_k, collection_name='product_docs'
            )
            return {'success': True, 'results': results}

        # 고객 문의 응대 (RAG)
        async def answer_customer_inquiry(question: str):
            """고객 문의에 대해 제품 문서 기반으로 답변합니다"""
            query_embedding = await asyncio.to_thread(openai_service.create_embedding, question)
            if not query_embedding:
                return {'success': False, 'error': '임베딩 생성 실패'}

            results = vectordb_service.search_documents(
                query_embedding, top_k=3, collection_name='product_docs'
            )
            if not results:
                return {
                    'success': True,
                    'answer': '관련 제품 문서를 찾을 수 없습니다. 고객센터로 문의해주세요.',
                    'sources': []
                }

            context_parts = []
            sources = []
            for r in results:
                context_parts.append(r.get('content', ''))
                sources.append(r.get('file_name', 'unknown'))

            context = "\n\n---\n\n".join(context_parts)

            answer = await asyncio.to_thread(
                openai_service.generate_text_with_system,
                system_prompt="""당신은 고객 서비스 전문가입니다.
제품 문서를 기반으로 고객의 질문에 친절하고 정확하게 답변하세요.

규칙:
1. 문서에 있는 정보만 사용하세요
2. 확실하지 않은 내용은 "확인 후 답변드리겠습니다"라고 안내하세요
3. 반품/교환/AS 관련은 정확한 절차를 안내하세요
4. 한국어로 친절하게 답변하세요""",
                user_prompt=f"제품 문서 내용:\n{context}\n\n고객 질문: {question}",
                temperature=0.3,
                max_tokens=1000,
            )

            return {'success': True, 'answer': answer, 'sources': list(set(sources))}

        # 제품 문서 목록
        async def list_product_documents():
            """업로드된 제품 문서 목록을 조회합니다"""
            return vectordb_service.list_documents(collection_name='product_docs')

        # 도구 등록
        self.register_tool('upload_product_document', upload_product_document,
                          '제품 관련 문서를 업로드합니다 (FAQ, 매뉴얼, 반품규정 등)')
        self.register_tool('search_product_documents', search_product_documents,
                          '제품 문서를 검색합니다 (query, top_k)')
        self.register_tool('answer_customer_inquiry', answer_customer_inquiry,
                          '고객 문의에 제품 문서 기반으로 답변합니다 (question)')
        self.register_tool('list_product_documents', list_product_documents,
                          '업로드된 제품 문서 목록을 조회합니다')

        # ─── Policy-driven actions (Ontology dispatch 용) ───
        self._register_policy_actions(user_id)

        print(f"[CS Agent] {len(self._tools)} tools, {len(self._action_handlers)} actions registered for user: {user_id}", file=sys.stderr)

    # ═══════════════════════════════════════════════════════════════
    # Policy-driven Actions (Ontology dispatch 용)
    # ═══════════════════════════════════════════════════════════════
    def _register_policy_actions(self, user_id: str = None):
        """ontology.yaml 의 delegate_to 가 호출하는 정책 기반 액션."""
        from ..services import vectordb_service, openai_service

        # ─────────────────────────────────────────────────────────
        # compose_reply — 고객 문의 답변 작성 (RAG, 발송은 EmailAgent.send_reply)
        # Type 3: RAG + 생성 LLM. 결과는 {subject, body, sources} 만 반환.
        # policy: {tone, language, max_length, include_sources?}
        # context: {payload: {subject, body}, customer}
        # ─────────────────────────────────────────────────────────
        async def compose_reply(policy: dict, context: dict) -> dict:
            payload = context.get("payload") or {}
            customer = context.get("customer") or {}
            question = (payload.get("body") or payload.get("subject") or "").strip()
            if not question:
                return {"action": "compose_reply", "success": False,
                        "error": "고객 문의 본문이 비어 있음"}

            tone = policy.get("tone", "professional")
            language = policy.get("language", "ko")
            max_length = int(policy.get("max_length", 1000))
            include_sources = bool(policy.get("include_sources", False))

            # (1) RAG: 제품 문서 검색
            sources: list = []
            context_text = ""
            try:
                query_embedding = await asyncio.to_thread(openai_service.create_embedding, question)
                if query_embedding:
                    results = vectordb_service.search_documents(
                        query_embedding, top_k=3, collection_name='product_docs'
                    ) or []
                    parts = []
                    for r in results:
                        parts.append(r.get('content', ''))
                        src = r.get('file_name')
                        if src and src not in sources:
                            sources.append(src)
                    context_text = "\n\n---\n\n".join(parts)
            except Exception as e:
                print(f"[CS Agent.compose_reply] RAG 실패(무시): {e}", file=sys.stderr)

            customer_name = (customer or {}).get("name") or payload.get("from_name") or "고객"
            tier = (customer or {}).get("tier") or "Standard"

            # (2) 답변 생성
            if language == "en":
                system_prompt = (
                    f"You are a customer support specialist writing a {tone} reply email. "
                    f"The customer tier is {tier}. Use the product documentation excerpts when relevant. "
                    f"If the docs don't cover the question, say so politely and promise a follow-up. "
                    f"Output: a clear subject line on the first line prefixed 'SUBJECT: ', then a blank line, then the body. "
                    f"Keep the body under {max_length} characters."
                )
            else:
                system_prompt = (
                    f"당신은 {tone} 톤으로 답변 메일을 작성하는 CS 전문가입니다. "
                    f"고객 등급: {tier}. 제품 문서 발췌가 있으면 활용해 정확하게 답하고, "
                    f"없거나 부족하면 정중히 확인 후 회신하겠다고 안내하세요. "
                    f"출력 형식: 1번째 줄에 'SUBJECT: 제목', 빈 줄, 그 다음 본문. "
                    f"본문은 {max_length}자 이내."
                )

            user_prompt = (
                f"제품 문서 발췌:\n{context_text or '(관련 문서 없음)'}\n\n"
                f"고객 이름: {customer_name}\n"
                f"문의 제목: {payload.get('subject', '')}\n"
                f"문의 내용:\n{question}"
            )

            try:
                raw = await asyncio.to_thread(
                    openai_service.generate_text_with_system,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    temperature=0.3,
                    max_tokens=1200,
                )
            except Exception as e:
                return {"action": "compose_reply", "success": False,
                        "error": f"답변 생성 실패: {e}"}

            # (3) SUBJECT/BODY 분리
            subject = ""
            body = raw or ""
            if raw:
                lines = raw.splitlines()
                if lines and lines[0].strip().upper().startswith("SUBJECT:"):
                    subject = lines[0].split(":", 1)[1].strip()
                    body = "\n".join(lines[1:]).lstrip("\n")

            if not subject:
                orig = payload.get("subject", "")
                subject = f"Re: {orig}" if orig else "Re: 문의 답변"

            if include_sources and sources:
                if language == "en":
                    body += "\n\n— References: " + ", ".join(sources)
                else:
                    body += "\n\n— 참고 문서: " + ", ".join(sources)

            return {
                "action": "compose_reply",
                "success": True,
                "subject": subject,
                "body": body,
                "sources": sources,
                "policy_applied": {
                    "tone": tone,
                    "language": language,
                    "max_length": max_length,
                    "include_sources": include_sources,
                    "tier": tier,
                },
                "note": "발송은 EmailAgent.send_reply 가 이 결과를 받아 처리",
            }

        self.register_action('compose_reply', compose_reply,
                             '제품 문서 RAG 기반 고객 답변 초안 작성 (subject/body 반환, 발송은 별도)')
