# mcp_server/ontology_engine/__init__.py
"""
OOSDK Ontology Engine

3개 핵심 컴포넌트:
- OntologyEngine (engine.py)     : 4 methods (resolve_links / check_rules / trigger_events / manage_memory)
- ThreeTierMemory (memory)       : hot / warm / cold
- SourceAdapter (adapters)       : Salesforce / LocalJson (소스 추상화)

외부에서 쓸 때:
    from mcp_server.ontology_engine import OntologyEngine, ThreeTierMemory
"""
from .engine import OntologyEngine
from .memory.facade import ThreeTierMemory

__all__ = ["OntologyEngine", "ThreeTierMemory"]
