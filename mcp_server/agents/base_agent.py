# mcp_server/agents/base_agent.py
"""
BaseAgent: 모든 전문 Agent의 기본 클래스
- LLM을 사용하여 자신의 도구(tools) 범위 내에서 판단/실행
- 실행 결과와 트레이싱 정보를 반환
"""
import time
import json
import sys
import asyncio
import logging
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

# Agent 전체 실행 제한 (mcp-remote 60초 timeout 대비 여유)
AGENT_RUN_TIMEOUT = 45  # seconds


# ════════════════════════════════════════════════════════════════
# Action / Tool 호출 로깅 헬퍼
# ════════════════════════════════════════════════════════════════
# Why: FastMCP `LoggingMiddleware` 는 외부에서 들어오는 MCP 호출만 인터셉트.
# `BaseAgent.execute_action()` / `execute_tool()` 은 ontology dispatch 안에서
# 일반 Python 메서드로 호출되기 때문에 미들웨어가 못 본다.
# → tool_logs 에 직접 insert 해서 dashboard "Agent 상태" 패널이 실제 실행을
#   반영하도록 한다. tool_name 컨벤션:
#     · execute_action  → "agent_action:<agent>.<action>"
#     · execute_tool    → "agent_tool:<agent>.<tool>"
#   (dashboard.py 의 get_agent_for_tool 이 prefix 로 agent 구분)

def _safe_jsonable(obj: Any) -> Any:
    """JSON 직렬화 가능한 형태로 강제 변환 (실패 시 str)."""
    try:
        json.dumps(obj, ensure_ascii=False, default=str)
        return obj
    except Exception:
        return str(obj)[:500]


def _summarize_for_log(result: Any, max_length: int = 200) -> str:
    """결과를 짧게 요약 — logging_middleware.summarize_result 와 동일 정책."""
    try:
        from mcp_server.logging_middleware import summarize_result
        return summarize_result(result, max_length=max_length)
    except Exception:
        try:
            return (json.dumps(result, ensure_ascii=False, default=str) or "")[:max_length]
        except Exception:
            return str(result)[:max_length]


def _log_internal_call(
    *,
    tool_name: str,
    parameters: Dict[str, Any],
    success: bool,
    duration_ms: float,
    error_message: Optional[str] = None,
    result_summary: Optional[str] = None,
    user_id: Optional[str] = None,
    client_type: str = "internal",
) -> None:
    """log_db / JSONL 에 내부 호출 1건 기록.

    LoggingMiddleware 가 안 잡는 경로 (BaseAgent.execute_action /
    execute_tool) 를 같은 store 에 흘려보내기 위한 fallback path.
    실패해도 본 비즈니스 로직은 그대로 진행 (best-effort 로깅).
    """
    try:
        # lazy import — 순환 참조 회피
        from mcp_server.logging_middleware import log_db, write_jsonl, summarize_result
        log_data = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "source": "remote",
            "client_type": client_type,
            "user_id": user_id,
            "tool_name": tool_name,
            "parameters": _safe_jsonable(parameters or {}),
            "success": bool(success),
            "error_message": error_message,
            "duration_ms": round(float(duration_ms), 2) if duration_ms is not None else None,
            "result_summary": result_summary,
        }
        log_db.insert_log(log_data)
        write_jsonl(log_data)
    except Exception as e:
        # 로깅 실패는 경고만 — 실행은 그대로 진행
        logger.debug(f"[base_agent._log_internal_call] 로그 기록 실패 ({tool_name}): {e}")


@dataclass
class AgentResult:
    """Agent 실행 결과"""
    agent_name: str
    success: bool
    result: Any = None
    error: Optional[str] = None
    steps: List[Dict] = field(default_factory=list)  # 실행 단계 추적
    duration_ms: float = 0
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return {
            'agent_name': self.agent_name,
            'success': self.success,
            'result': self.result,
            'error': self.error,
            'steps': self.steps,
            'duration_ms': self.duration_ms,
            'timestamp': self.timestamp,
        }


class BaseAgent:
    """전문 Agent 기본 클래스"""

    def __init__(self, name: str, description: str, llm_config: dict):
        self.name = name
        self.description = description
        self.llm_config = llm_config
        self._tools = {}  # {tool_name: callable}
        self._tool_descriptions = {}  # {tool_name: description}
        # ─── Policy-driven action handlers (Ontology dispatch 용) ───
        # 자유 요청 (run/think) 과 분리. 정책이 WHAT 을 결정한 액션 전용.
        # 핸들러는 (policy: dict, context: dict) 시그니처로 호출됨.
        self._action_handlers = {}  # {action_name: callable}
        self._action_descriptions = {}  # {action_name: description}

    def register_tool(self, name: str, func: callable, description: str = ""):
        """Agent에 도구 등록"""
        self._tools[name] = func
        self._tool_descriptions[name] = description
        print(f"[{self.name}] Tool registered: {name}", file=sys.stderr)

    # ═══════════════════════════════════════════════════════════════
    # Policy-driven Action API (Ontology dispatch 용)
    # ═══════════════════════════════════════════════════════════════
    # 자유 요청 (run → think → execute_tool) 과 다른 진입점.
    # Ontology 가 정책으로 "어느 agent / 어떤 action / 어떤 policy" 를
    # 이미 결정한 경우, agent 의 LLM think() 를 건너뛰고 직접 핸들러를 실행.
    #
    # 핸들러 종류:
    #   - Type 1 (Pure code)         : 도구 시퀀스가 비즈니스로 명확. LLM 0회.
    #   - Type 2 (Code + 생성 LLM)   : 시퀀스는 코드, 텍스트 생성만 LLM.
    #   - Type 3 (think 위임)        : 입력 의존적이라 핸들러 안에서 self.think 사용.

    def register_action(self, name: str, handler: callable, description: str = ""):
        """정책 기반 액션 핸들러 등록.

        Args:
            name: 액션 이름 (ontology.yaml 의 delegate_to.action 과 매칭)
            handler: async (policy: dict, context: dict) -> dict
            description: 액션 설명 (디버깅/감사용)
        """
        self._action_handlers[name] = handler
        self._action_descriptions[name] = description
        print(f"[{self.name}] Action registered: {name}", file=sys.stderr)

    def get_available_actions(self) -> List[str]:
        return list(self._action_handlers.keys())

    async def execute_action(
        self,
        action_name: str,
        policy: Optional[Dict] = None,
        context: Optional[Dict] = None,
    ) -> Dict:
        """정책 기반 액션 실행. think() 없이 핸들러 직접 호출.

        Returns:
            {"success": bool, "agent": str, "action": str, "result": ...,
             "duration_ms": float}  또는 에러 시 {"success": False, "error": str, ...}

        Logging: 이 호출은 FastMCP 미들웨어를 거치지 않으므로 직접
        `tool_logs` 에 기록한다 (tool_name = "agent_action:<agent>.<action>").
        Dashboard "Agent 상태" 패널이 실제 dispatch 를 반영하기 위함.
        """
        # log_tool_name 은 unknown action 에러 케이스에서도 기록
        log_tool_name = f"agent_action:{self.name}.{action_name}"
        # context 에서 user_id 가 있으면 같이 기록 (filter dropdown 사용)
        ctx_user_id = (context or {}).get("user_id") if isinstance(context, dict) else None

        if action_name not in self._action_handlers:
            err = (
                f"Unknown action: {action_name}. "
                f"Available: {list(self._action_handlers.keys())}"
            )
            _log_internal_call(
                tool_name=log_tool_name,
                parameters={"policy": policy or {}, "available": list(self._action_handlers.keys())},
                success=False,
                duration_ms=0,
                error_message=err,
                user_id=ctx_user_id,
            )
            return {
                "success": False,
                "agent": self.name,
                "action": action_name,
                "error": err,
            }

        policy = policy or {}
        context = context or {}
        start_time = time.time()
        try:
            result = await self._action_handlers[action_name](
                policy=policy, context=context
            )
            duration_ms = (time.time() - start_time) * 1000
            _log_internal_call(
                tool_name=log_tool_name,
                parameters={"policy": policy},
                success=True,
                duration_ms=duration_ms,
                result_summary=_summarize_for_log(result),
                user_id=ctx_user_id,
            )
            return {
                "success": True,
                "agent": self.name,
                "action": action_name,
                "result": result,
                "duration_ms": round(duration_ms, 2),
            }
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            print(f"[{self.name}] Action error ({action_name}): {e}", file=sys.stderr)
            _log_internal_call(
                tool_name=log_tool_name,
                parameters={"policy": policy},
                success=False,
                duration_ms=duration_ms,
                error_message=str(e),
                user_id=ctx_user_id,
            )
            return {
                "success": False,
                "agent": self.name,
                "action": action_name,
                "error": str(e),
                "duration_ms": round(duration_ms, 2),
            }

    def get_available_tools(self) -> List[str]:
        """사용 가능한 도구 목록"""
        return list(self._tools.keys())

    def get_tools_description(self) -> str:
        """LLM에 전달할 도구 설명 문자열"""
        lines = []
        for name, desc in self._tool_descriptions.items():
            lines.append(f"- {name}: {desc}")
        return "\n".join(lines)

    async def execute_tool(self, tool_name: str, **kwargs) -> Dict:
        """도구 실행 (에러 핸들링 포함).

        Logging: FastMCP 미들웨어가 안 잡는 내부 호출이므로 같은 store 에
        `agent_tool:<agent>.<tool>` 컨벤션으로 기록한다.
        """
        log_tool_name = f"agent_tool:{self.name}.{tool_name}"
        # kwargs 에 user_id 가 있으면 사용 (없으면 None)
        ctx_user_id = kwargs.get("user_id")

        if tool_name not in self._tools:
            err = f"Unknown tool: {tool_name}. Available: {list(self._tools.keys())}"
            _log_internal_call(
                tool_name=log_tool_name,
                parameters=kwargs,
                success=False,
                duration_ms=0,
                error_message=err,
                user_id=ctx_user_id,
            )
            return {
                'success': False,
                'error': err,
            }

        start_time = time.time()
        try:
            result = await self._tools[tool_name](**kwargs)
            duration_ms = (time.time() - start_time) * 1000
            _log_internal_call(
                tool_name=log_tool_name,
                parameters=kwargs,
                success=True,
                duration_ms=duration_ms,
                result_summary=_summarize_for_log(result),
                user_id=ctx_user_id,
            )
            return {
                'success': True,
                'tool': tool_name,
                'result': result,
                'duration_ms': round(duration_ms, 2),
            }
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            print(f"[{self.name}] Tool error ({tool_name}): {e}", file=sys.stderr)
            _log_internal_call(
                tool_name=log_tool_name,
                parameters=kwargs,
                success=False,
                duration_ms=duration_ms,
                error_message=str(e),
                user_id=ctx_user_id,
            )
            return {
                'success': False,
                'tool': tool_name,
                'error': str(e),
                'duration_ms': round(duration_ms, 2),
            }

    async def think(self, task: str, context: dict = None) -> dict:
        """
        LLM을 사용하여 작업 계획 수립
        - 어떤 도구를 어떤 순서로 실행할지 결정
        Returns: {'plan': [...], 'reasoning': '...'}
        """
        from ..services.openai_service import generate_text_with_system

        tools_desc = self.get_tools_description()
        system_prompt = f"""당신은 '{self.name}' 전문 에이전트입니다.
역할: {self.description}

사용 가능한 도구:
{tools_desc}

사용자의 요청을 분석하여, 어떤 도구를 어떤 순서로 실행해야 하는지 JSON으로 계획을 세우세요.

반드시 아래 형식으로만 응답하세요:
{{
  "reasoning": "작업 분석 설명",
  "plan": [
    {{"tool": "도구이름", "params": {{"param1": "value1"}}, "description": "이 단계의 목적"}}
  ]
}}

도구 실행이 필요 없는 경우:
{{
  "reasoning": "설명",
  "plan": [],
  "direct_answer": "직접 답변 내용"
}}"""

        user_prompt = f"요청: {task}"
        if context:
            user_prompt += f"\n추가 정보: {json.dumps(context, ensure_ascii=False)}"

        try:
            # generate_text_with_system은 동기 함수 → asyncio.to_thread로 비동기 실행
            response = await asyncio.to_thread(
                generate_text_with_system,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=self.llm_config.get('temperature', 0.3),
                max_tokens=self.llm_config.get('max_tokens', 2000),
            )

            # JSON 파싱 시도
            cleaned = response.strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            if cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]

            plan = json.loads(cleaned.strip())
            return plan

        except json.JSONDecodeError as e:
            print(f"[{self.name}] LLM response parse error: {e}", file=sys.stderr)
            return {
                'reasoning': f'LLM 응답 파싱 실패: {response[:200]}',
                'plan': [],
                'direct_answer': response,
            }
        except Exception as e:
            print(f"[{self.name}] Think error: {e}", file=sys.stderr)
            return {
                'reasoning': f'오류 발생: {str(e)}',
                'plan': [],
            }

    async def run(self, task: str, context: dict = None) -> AgentResult:
        """
        Agent 메인 실행 루프:
        1. think() → 계획 수립
        2. 계획에 따라 도구 순차 실행
        3. 결과 종합하여 반환

        전체 실행은 AGENT_RUN_TIMEOUT(45초) 내에 완료되어야 함
        (mcp-remote 60초 timeout 대비)
        """
        start_time = time.time()

        try:
            return await asyncio.wait_for(
                self._run_internal(task, context, start_time),
                timeout=AGENT_RUN_TIMEOUT,
            )
        except asyncio.TimeoutError:
            duration_ms = (time.time() - start_time) * 1000
            print(f"[{self.name}] ⏰ TIMEOUT after {duration_ms:.0f}ms (limit: {AGENT_RUN_TIMEOUT}s)", file=sys.stderr)
            return AgentResult(
                agent_name=self.name,
                success=False,
                error=f"Agent 실행 시간 초과 ({AGENT_RUN_TIMEOUT}초). 작업을 더 작은 단위로 나눠주세요.",
                steps=[{'step': 'timeout', 'duration_ms': round(duration_ms, 2)}],
                duration_ms=round(duration_ms, 2),
            )
        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            print(f"[{self.name}] ❌ Unexpected error: {e}", file=sys.stderr)
            return AgentResult(
                agent_name=self.name,
                success=False,
                error=f"Agent 실행 오류: {str(e)}",
                steps=[{'step': 'error', 'error': str(e)}],
                duration_ms=round(duration_ms, 2),
            )

    async def _run_internal(self, task: str, context: dict, start_time: float) -> AgentResult:
        """실제 실행 로직 (timeout으로 감싸짐)"""
        steps = []

        print(f"\n[{self.name}] === Task received: {task[:100]}... ===", file=sys.stderr)

        # Step 1: 계획 수립
        print(f"[{self.name}] 🧠 Planning...", file=sys.stderr)
        plan_result = await self.think(task, context)
        plan_elapsed = (time.time() - start_time) * 1000
        print(f"[{self.name}] 🧠 Planning done ({plan_elapsed:.0f}ms)", file=sys.stderr)

        steps.append({
            'step': 'planning',
            'reasoning': plan_result.get('reasoning', ''),
            'plan': plan_result.get('plan', []),
            'duration_ms': round(plan_elapsed, 2),
        })

        # 직접 답변인 경우
        if plan_result.get('direct_answer'):
            duration_ms = (time.time() - start_time) * 1000
            return AgentResult(
                agent_name=self.name,
                success=True,
                result=plan_result['direct_answer'],
                steps=steps,
                duration_ms=round(duration_ms, 2),
            )

        # Step 2: 계획에 따라 도구 실행
        tool_results = []
        plan = plan_result.get('plan', [])

        for i, step in enumerate(plan):
            tool_name = step.get('tool')
            params = step.get('params', {})
            description = step.get('description', '')

            remaining = AGENT_RUN_TIMEOUT - (time.time() - start_time)
            if remaining < 5:
                print(f"[{self.name}] ⚠️ Skipping step {i+1} - only {remaining:.1f}s remaining", file=sys.stderr)
                steps.append({
                    'step': f'skipped_{i+1}',
                    'tool': tool_name,
                    'reason': f'시간 부족 ({remaining:.1f}s 남음)',
                })
                break

            print(f"[{self.name}] Step {i+1}/{len(plan)}: {tool_name} - {description} (남은시간: {remaining:.1f}s)", file=sys.stderr)

            result = await self.execute_tool(tool_name, **params)
            tool_results.append(result)

            steps.append({
                'step': f'execute_{i+1}',
                'tool': tool_name,
                'params': params,
                'description': description,
                'success': result.get('success', False),
                'duration_ms': result.get('duration_ms', 0),
            })

            # 실패 시 중단 여부 판단
            if not result.get('success'):
                print(f"[{self.name}] Step {i+1} failed: {result.get('error')}", file=sys.stderr)

        # Step 3: 결과 종합
        duration_ms = (time.time() - start_time) * 1000
        all_success = all(r.get('success', False) for r in tool_results) if tool_results else True

        # 결과를 의미 있게 종합
        combined_result = {
            'tool_results': tool_results,
            'summary': f"{self.name}이(가) {len(plan)}개 작업 중 "
                      f"{sum(1 for r in tool_results if r.get('success'))}개 성공",
        }

        print(f"[{self.name}] ✅ Completed in {duration_ms:.0f}ms", file=sys.stderr)

        return AgentResult(
            agent_name=self.name,
            success=all_success,
            result=combined_result,
            steps=steps,
            duration_ms=round(duration_ms, 2),
        )

    def __repr__(self):
        return f"<{self.name} tools={self.get_available_tools()}>"
