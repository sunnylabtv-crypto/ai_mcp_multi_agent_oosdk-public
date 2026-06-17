# OOSDK — Ontology-Oriented Multi-Agent Platform

**Business strategy as code.** OOSDK drives a multi-agent system from a single **ontology** (`ontology.yaml`) that encodes a company's policies and decision rules. Change one line of policy and the agents' collaboration and branching change — **no code redeploy**. The ontology defines *WHAT* (policy/intent); the agents handle *HOW* (execution) — so routine decisions can be made deterministically by policy rather than by an LLM call.

> The flagship of the **SunnyLab** build series. This is a **sanitized public showcase** — credentials, tokens, and infrastructure identifiers (GCP project, VM IP, Odoo tenant) were removed before publishing. Some modules require your own Odoo/Salesforce/GCP configuration to run end to end.

## Core idea
```
ontology.yaml  (policy / strategy, human-editable)
        │   "WHAT to do, under which policy"
        ▼
Ontology Engine ── deterministic policy decisions ──►  Agents ("HOW", execution)
        │                                                 ├─ sales / crm / erp / inventory
        │                                                 ├─ cs / helpdesk / email / calendar
        └─ when needed: LLM reasoning + RAG               └─ analytics / report
```
- **Policy-driven dispatch** — many decisions need *zero* LLM calls (cost + determinism)
- **Extensible by design** — add a new domain agent on the same base; the ontology wires it in

## Business Case (BC) series — end-to-end automation
A sales funnel automated across stages, integrating **Salesforce (SFDC)** and **Odoo ERP**:
- **BC2 Sales** — lead convert, pricing/quote
- **BC3 Fulfillment** — order → inventory → shipping, Odoo automation
- **BC4 Inventory allocation** — deterministic A/B/C priority + override
- **BC5 Replenishment** — autonomous purchase/replenishment with a manager briefing

## Key capabilities
- **Ontology engine** that encodes business policy and drives multi-agent collaboration
- **Multi-domain agents** (sales, CRM, ERP, inventory, CS, helpdesk, email, calendar, analytics, report) over **MCP / FastMCP**
- **Enterprise integration** — SFDC + Odoo ERP adapters; **RAG (ChromaDB)**; 3-tier memory (hot/warm/cold)
- **Bilingual Streamlit dashboard** (KR/EN) — decisions, inventory, ontology stats
- **Cloud-native** — Docker, Cloud Build, GitHub Actions (project/VM values are placeholders)

## Tech stack
Python · MCP / FastMCP · Ontology-driven orchestration · Salesforce & Odoo ERP · ChromaDB (RAG) · Streamlit · Docker · Google Cloud · GitHub Actions

## Project structure
```
ontology/            # ontology.yaml — business policy as code
mcp_server/          # ontology engine, domain agents, tools, adapters (SFDC/Odoo)
dashboard_modules/   # dashboard components
dashboard.py / dashboard_en.py   # Streamlit dashboards (KR/EN)
scripts/             # BC2-BC5 business-case demos & setup
docs/                # design notes / specs
tests/               # unit tests
.env.example         # required env vars (no real keys)
```

## Setup
```bash
cp .env.example .env      # configure OpenAI/Google, Salesforce, Odoo (your own)
pip install -r requirements.txt
# run the MCP server (see mcp_server/) and a dashboard:
streamlit run dashboard.py
```

## Note
Public **portfolio showcase** of an actively evolving project. For safety, all secrets/credentials and infra identifiers were stripped; external integrations (Odoo, Salesforce, GCP) require your own configuration. Architecture write-ups and demos: SunnyLab below.

---
**SunnyLab** — building agentic AI in public · Medium [@sunnylabtv](https://medium.com/@sunnylabtv) · YouTube [@sunnylabtv](https://www.youtube.com/@sunnylabtv)
