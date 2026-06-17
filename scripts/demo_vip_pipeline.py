# scripts/demo_vip_pipeline.py
"""
OOSDK Phase 1 데모 — VIP / Standard / New Prospect 파이프라인

실행:
    python -m scripts.demo_vip_pipeline

흐름:
    for each test email:
        1. resolve_links()   → Person + Customer (SFDC or mock)
        2. check_rules()     → 어떤 rule 매칭됐나
        3. trigger_events()  → 실행 계획
        4. manage_memory()   → 결과를 3-tier 에 저장

출력:
    각 단계별 trace 를 stdout 에 예쁘게 출력.
    실제 agent 는 호출하지 않음 (계획만 만듦).
"""
import json
import os
import sys
from pathlib import Path

# 프로젝트 루트를 sys.path 에 추가
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from mcp_server.ontology_engine import OntologyEngine, ThreeTierMemory  # noqa: E402


# 색상 (터미널용)
class C:
    HEADER = "\033[95m"
    BLUE = "\033[94m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    END = "\033[0m"
    BOLD = "\033[1m"


def banner(text: str, char: str = "="):
    line = char * 70
    print(f"\n{C.BOLD}{C.BLUE}{line}\n {text}\n{line}{C.END}")


def subbanner(text: str):
    print(f"\n{C.BOLD}{C.YELLOW}▶ {text}{C.END}")


def print_dict(label: str, d, indent: int = 2):
    s = json.dumps(d, ensure_ascii=False, indent=indent, default=str)
    print(f"  {C.GREEN}{label}{C.END}")
    for line in s.split("\n"):
        print(f"    {line}")


def main(force_mock: bool = True):
    """
    Args:
        force_mock: True 면 ontology.yaml 을 로드한 후 Customer 의 source 를
                    local_json 으로 강제. SFDC 인증 없이 데모 가능.
    """
    banner("OOSDK Phase 1 — VIP / Standard / New Prospect Demo")

    yaml_path = PROJECT_ROOT / "ontology" / "ontology.yaml"
    emails_path = PROJECT_ROOT / "ontology" / "mock_data" / "test_emails.json"
    mock_customers = PROJECT_ROOT / "ontology" / "mock_data" / "mock_customers.json"

    # 메모리 기본 경로 — env 로 오버라이드 가능 (NFS/fuse 마운트 이슈 대응)
    mem_dir = Path(os.environ.get("OOSDK_MEMORY_DIR", str(PROJECT_ROOT / "data" / "memory")))
    memory = ThreeTierMemory({
        "hot":  {"backend": "in_memory", "ttl_sec": 3600, "max_size": 1000},
        "warm": {"backend": "sqlite",    "ttl_sec": 2592000,
                 "path": str(mem_dir / "warm.db")},
        "cold": {"backend": "jsonl",
                 "path": str(mem_dir / "cold")},
    })

    # 엔진 초기화
    engine = OntologyEngine(str(yaml_path), memory=memory)

    # 데모 안정성: Customer 어댑터를 local_json 으로 강제
    if force_mock:
        from mcp_server.ontology_engine.adapters import LocalJsonAdapter
        engine.adapters["Customer"] = LocalJsonAdapter(
            {
                "type": "local_json",
                "path": str(mock_customers),
                "lookup": {"by": "email_domain"},
            }
        )
        print(f"{C.YELLOW}  ⚠ force_mock=True — Customer 를 local_json 으로 강제 사용{C.END}")
        print(f"    path: {mock_customers}")

    # 테스트 이메일 로드
    with open(emails_path, encoding="utf-8") as f:
        emails = json.load(f)

    # 각 이메일 처리
    for idx, email in enumerate(emails, 1):
        banner(f"[{idx}/{len(emails)}] {email['from']}  |  기대: {email['expected_rule']}", "─")

        # Step 1: resolve_links
        subbanner("1. resolve_links()")
        ctx = engine.resolve_links("email", email)
        print_dict("person", ctx.get("person"))
        print_dict("customer", ctx.get("customer"))

        # Step 2: check_rules
        subbanner("2. check_rules()")
        action = engine.check_rules(ctx)
        if action:
            matched = action.get("rule_name")
            mark = "✓" if matched == email["expected_rule"] else "✗"
            color = C.GREEN if matched == email["expected_rule"] else C.RED
            print(f"  {color}{mark} matched rule: {matched}{C.END}")
            print_dict("action", {k: v for k, v in action.items() if k != "rule_name"})
        else:
            print(f"  {C.RED}✗ no rule matched{C.END}")

        # Step 3: trigger_events
        subbanner("3. trigger_events()")
        plan = engine.trigger_events(action, ctx)
        if plan:
            for step_idx, p in enumerate(plan, 1):
                print(f"  {step_idx}. [{p['agent']}] {p['tool']}")
                if p.get("params"):
                    print(f"       params: {json.dumps(p['params'], ensure_ascii=False)}")
        else:
            print("  (empty plan)")

        # Step 4: manage_memory
        subbanner("4. manage_memory()")
        memory_tier = action.get("memory_tier", "hot") if action else "hot"
        memory_key = f"email_trace:{email['id']}"
        engine.manage_memory(
            memory_key,
            {
                "email": email,
                "matched_rule": action.get("rule_name") if action else None,
                "event_plan": plan,
            },
            tier=memory_tier,
        )
        recalled = engine.recall_memory(memory_key, tier=memory_tier)
        print(f"  stored in tier={C.BOLD}{memory_tier}{C.END}, key={memory_key}")
        print(f"  recalled OK? {'✓' if recalled else '✗'}")

    # 메모리 요약
    banner("Memory Summary")
    stats = memory.stats()
    for tier, info in stats.items():
        print(f"  {C.BOLD}{tier:5s}{C.END} size={info['size']:4d}  backend={info['backend']}")

    print(f"\n{C.GREEN}{C.BOLD}✓ Demo 완료{C.END}\n")


if __name__ == "__main__":
    force_mock = os.environ.get("OOSDK_FORCE_MOCK", "true").lower() != "false"
    main(force_mock=force_mock)
