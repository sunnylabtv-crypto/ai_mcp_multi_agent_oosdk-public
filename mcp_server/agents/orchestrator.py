# mcp_server/agents/orchestrator.py
"""
Orchestrator (DEPRECATED in OOSDK Phase 2.1+)
─────────────────────────────────────────────
이 클래스는 과거 LLM 기반 "내부 팀장" 역할을 하던 모듈입니다.
v1.2 (Policy-driven Multi-Agent Dispatch) 부터는 다음과 같이 책임이 재배치됨:

  • CEO 레이어 (1st layer LLM)  : Claude Desktop. 자유 요청을 직접 분석/위임.
  • 팀장 레이어 (2nd layer LLM)  : 각 도메인 Agent (Email/CRM/Calendar/CS/...).
                                    필요 시 자체 LLM(think) 으로 도구 선택/파라미터 생성.
  • 매뉴얼 레이어               : ontology.yaml 의 rules + delegate_to.
                                    정책 기반 dispatch (LLM 0회 또는 최소).

따라서 "내부 LLM 오케스트레이터" 는 더 이상 결정 경로에 있지 않습니다.

본 파일은 다음 두 가지 호환성 사유로만 보존됩니다:
  1) Agent registry: register_agent / get_registered_agents — server.py 가
     사용자 정보/통계 MCP 도구에 사용.
  2) execution history: 과거 호출 이력 누적기 — 일부 디버그 도구가 참조.

`analyze_request()` / `execute()` 는 호출 시 deprecation warning 만 남기고 결과만 반환합니다.
신규 dispatch 는 반드시 `OntologyEngine.trigger_events()` + `BaseAgent.execute_action()` 경로 사용.
"""
import json
import time
import sys
import asyncio
import warnings
from typing import Dict, List, Optional, Any
from datetime import datetime

from .base_agent import BaseAgent, AgentResult


class Orchestrator:
    """
    Orchestrator Agent — Agent registry / history holder (DEPRECATED for LLM dispatch).

    OOSDK Phase 2.1+ 에서는 dispatch 결정을 ontology.yaml 의 delegate_to 와
    BaseAgent.execute_action() 이 담당합니다. 이 클래스의 LLM 메서드들
    (analyze_request, execute) 는 호출되면 DeprecationWarning 을 발생시킵니다.

    유지되는 책임:
      - register_agent / get_registered_agents (Agent registry)
      - get_execution_history (디버그용 누적기)

    흐름:
    1. 사용자 요청 수신
    2. LLM으로 요청 분석 → 어떤 Agent가 필요한지 결정
    3. 해당 Agent(들)에게 작업 위임
    4. 결과 종합하여 최종 응답 생성
    """

    def __init__(self, llm_config: dict):
        self.name = "Orchestrator"
        self.llm_config = llm_config
        self._agents: Dict[str, BaseAgent] = {}
        self._execution_history: List[Dict] = []

    def register_agent(self, agent_id: str, agent: BaseAgent):
        """전문 Agent 등록"""
        self._agents[agent_id] = agent
        print(f"[Orchestrator] Agent registered: {agent_id} ({agent.name})", file=sys.stderr)

    def get_agents_summary(self) -> str:
        """등록된 Agent들의 요약 정보"""
        lines = []
        for agent_id, agent in self._agents.items():
            tools = agent.get_available_tools()
            lines.append(f"- {agent_id} ({agent.name}): {agent.description}")
            lines.append(f"  도구: {', '.join(tools)}")
        return "\n".join(lines)

    async def analyze_request(self, user_request: str, context: dict = None) -> dict:
        """
        [DEPRECATED] LLM 기반 요청 분석.
        OOSDK Phase 2.1+ 에서는 Claude Desktop (CEO) 가 직접 위임을 결정하거나,
        ontology.yaml 의 rules 가 결정합니다. 이 메서드는 호환성 유지용입니다.

        Returns: {
            'reasoning': '분석 설명',
            'delegations': [...],
            'execution_mode': 'sequential' | 'parallel'
        }
        """
        warnings.warn(
            "Orchestrator.analyze_request() 는 deprecated 입니다. "
            "Claude Desktop 또는 ontology.yaml 기반 dispatch 를 사용하세요.",
            DeprecationWarning, stacklevel=2,
        )
        print("[Orchestrator] ⚠️ analyze_request() called (deprecated path)", file=sys.stderr)
        from ..services.openai_service import generate_text_with_system

        agents_desc = self.get_agents_summary()

        system_prompt = f"""당신은 Multi-Agent 시스템의 Orchestrator(팀장)입니다.
사용자의 요청을 분석하여, 어떤 전문 Agent에게 어떤 작업을 맡길지 결정합니다.

등록된 Agent:
{agents_desc}

응답 규칙:
1. 반드시 JSON 형식으로만 응답하세요
2. 여러 Agent가 필요하면 모두 지정하세요
3. 의존성이 있으면 depends_on으로 지정하세요 (예: CRM Agent는 Email Agent 결과가 필요)
4. 병렬 실행 가능하면 execution_mode를 'parallel'로 설정
5. Agent가 필요 없는 단순 질문이면 delegations를 비워두고 direct_answer를 작성

응답 형식:
{{
  "reasoning": "요청 분석 설명",
  "delegations": [
    {{
      "agent_id": "agent_id",
      "task": "Agent에게 전달할 구체적 작업 설명",
      "priority": 1,
      "depends_on": null
    }}
  ],
  "execution_mode": "sequential",
  "direct_answer": null
}}"""

        user_prompt = f"사용자 요청: {user_request}"
        if context:
            user_prompt += f"\n추가 컨텍스트: {json.dumps(context, ensure_ascii=False)}"

        try:
            response = await asyncio.to_thread(
                generate_text_with_system,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.2,
                max_tokens=2000,
            )

            cleaned = response.strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            if cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]

            return json.loads(cleaned.strip())

        except Exception as e:
            print(f"[Orchestrator] Analysis error: {e}", file=sys.stderr)
            return {
                'reasoning': f'분석 오류: {str(e)}',
                'delegations': [],
                'execution_mode': 'sequential',
                'direct_answer': f'요청 처리 중 오류가 발생했습니다: {str(e)}',
            }

    async def execute(self, user_request: str, context: dict = None) -> dict:
        """
        [DEPRECATED] LLM 기반 메인 실행 흐름.
        OOSDK Phase 2.1+ 에서는 server.py 의 process_with_ontology() 또는
        Claude Desktop 의 직접 도구 호출이 이 역할을 대체합니다.
        """
        warnings.warn(
            "Orchestrator.execute() 는 deprecated 입니다. "
            "process_with_ontology() 또는 Claude Desktop 직접 dispatch 를 사용하세요.",
            DeprecationWarning, stacklevel=2,
        )
        print("[Orchestrator] ⚠️ execute() called (deprecated path)", file=sys.stderr)
        start_time = time.time()
        execution_id = datetime.now().strftime('%Y%m%d_%H%M%S_%f')

        print(f"\n{'='*60}", file=sys.stderr)
        print(f"[Orchestrator] Execution #{execution_id}", file=sys.stderr)
        print(f"[Orchestrator] Request: {user_request[:200]}", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)

        # Step 1: 요청 분석
        analysis = await self.analyze_request(user_request, context)
        print(f"[Orchestrator] Analysis: {analysis.get('reasoning', '')}", file=sys.stderr)

        # 직접 답변인 경우
        if analysis.get('direct_answer') and not analysis.get('delegations'):
            duration_ms = (time.time() - start_time) * 1000
            result = {
                'execution_id': execution_id,
                'success': True,
                'analysis': analysis,
                'agent_results': [],
                'final_answer': analysis['direct_answer'],
                'duration_ms': round(duration_ms, 2),
            }
            self._execution_history.append(result)
            return result

        # Step 2: Agent들에게 위임
        delegations = analysis.get('delegations', [])
        execution_mode = analysis.get('execution_mode', 'sequential')
        agent_results: List[AgentResult] = []
        agent_outputs: Dict[str, Any] = {}  # Agent 간 데이터 전달용

        if execution_mode == 'parallel':
            # 병렬 실행 (의존성 없는 것들)
            import asyncio
            tasks = []
            for delegation in delegations:
                agent_id = delegation['agent_id']
                if agent_id in self._agents and not delegation.get('depends_on'):
                    agent = self._agents[agent_id]
                    task_context = {**(context or {}), 'agent_outputs': agent_outputs}
                    tasks.append(agent.run(delegation['task'], task_context))

            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for i, result in enumerate(results):
                    if isinstance(result, Exception):
                        agent_results.append(AgentResult(
                            agent_name=delegations[i]['agent_id'],
                            success=False,
                            error=str(result),
                        ))
                    else:
                        agent_results.append(result)
                        agent_outputs[result.agent_name] = result.to_dict()

            # 의존성이 있는 것들은 순차 실행
            for delegation in delegations:
                if delegation.get('depends_on'):
                    agent_id = delegation['agent_id']
                    if agent_id in self._agents:
                        agent = self._agents[agent_id]
                        task_context = {**(context or {}), 'agent_outputs': agent_outputs}
                        result = await agent.run(delegation['task'], task_context)
                        agent_results.append(result)
                        agent_outputs[result.agent_name] = result.to_dict()
        else:
            # 순차 실행
            for delegation in delegations:
                agent_id = delegation['agent_id']
                if agent_id not in self._agents:
                    print(f"[Orchestrator] Unknown agent: {agent_id}", file=sys.stderr)
                    agent_results.append(AgentResult(
                        agent_name=agent_id,
                        success=False,
                        error=f"Unknown agent: {agent_id}",
                    ))
                    continue

                agent = self._agents[agent_id]
                task_context = {**(context or {}), 'agent_outputs': agent_outputs}
                result = await agent.run(delegation['task'], task_context)
                agent_results.append(result)
                agent_outputs[result.agent_name] = result.to_dict()

                print(f"[Orchestrator] {agent.name} completed: success={result.success}", file=sys.stderr)

        # Step 3: 결과 종합
        final_answer = await self._synthesize_results(
            user_request, analysis, agent_results
        )

        duration_ms = (time.time() - start_time) * 1000
        all_success = all(r.success for r in agent_results) if agent_results else True

        result = {
            'execution_id': execution_id,
            'success': all_success,
            'analysis': {
                'reasoning': analysis.get('reasoning', ''),
                'execution_mode': execution_mode,
                'agents_used': [d['agent_id'] for d in delegations],
            },
            'agent_results': [r.to_dict() for r in agent_results],
            'final_answer': final_answer,
            'duration_ms': round(duration_ms, 2),
            'timestamp': datetime.now().isoformat(),
        }

        self._execution_history.append(result)
        print(f"\n[Orchestrator] Execution completed in {result['duration_ms']}ms", file=sys.stderr)
        return result

    async def _synthesize_results(
        self, user_request: str, analysis: dict, agent_results: List[AgentResult]
    ) -> str:
        """여러 Agent의 결과를 종합하여 최종 답변 생성"""
        from ..services.openai_service import generate_text_with_system

        # 결과 요약 생성
        results_summary = []
        for r in agent_results:
            result_data = r.result if isinstance(r.result, str) else json.dumps(r.result, ensure_ascii=False, default=str)
            results_summary.append({
                'agent': r.agent_name,
                'success': r.success,
                'result': result_data[:1000],  # 길이 제한
                'error': r.error,
                'duration_ms': r.duration_ms,
            })

        system_prompt = """당신은 Multi-Agent 시스템의 Orchestrator입니다.
여러 전문 Agent들의 실행 결과를 종합하여, 사용자에게 명확하고 유용한 최종 답변을 작성하세요.

규칙:
1. 각 Agent의 결과를 빠짐없이 포함
2. 실패한 작업이 있으면 명확히 언급
3. 한국어로 자연스럽게 작성
4. 필요시 다음 단계 제안"""

        user_prompt = f"""원래 요청: {user_request}

분석: {analysis.get('reasoning', '')}

Agent 결과:
{json.dumps(results_summary, ensure_ascii=False, indent=2)}

위 결과를 종합하여 사용자에게 전달할 최종 답변을 작성하세요."""

        try:
            return await asyncio.to_thread(
                generate_text_with_system,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.3,
                max_tokens=2000,
            )
        except Exception as e:
            # LLM 실패 시 기본 요약
            lines = [f"## 실행 결과\n"]
            for r in agent_results:
                status = "✅ 성공" if r.success else "❌ 실패"
                lines.append(f"- **{r.agent_name}**: {status} ({r.duration_ms}ms)")
                if r.error:
                    lines.append(f"  오류: {r.error}")
            return "\n".join(lines)

    def get_execution_history(self, limit: int = 10) -> List[dict]:
        """최근 실행 이력"""
        return self._execution_history[-limit:]

    def get_registered_agents(self) -> Dict[str, dict]:
        """등록된 Agent 정보"""
        result = {}
        for agent_id, agent in self._agents.items():
            result[agent_id] = {
                'name': agent.name,
                'description': agent.description,
                'tools': agent.get_available_tools(),
            }
        return result
