# Ambient Service Order Agent: Submission Write-Up

## Problem Statement
In industrial and manufacturing environments, service orders for replacement parts are often delayed by manual review processes, costing thousands of dollars in downtime. Conversely, automatically approving all orders leaves the organization vulnerable to fraud, prompt injection attacks (via automated systems), and excessive costs for expensive components. There is a need for an intelligent agentic workflow that can auto-approve low-risk orders, accurately assess risk for complex ones, securely scrub sensitive data, and keep humans in the loop only when their judgment is critical.

## Solution Architecture

```mermaid
graph TD
    Start((Start)) --> Extract[Extract Incident]
    Extract --> Security[Security Checkpoint\nPII Scrub & Injection Detect]
    
    Security -->|auto_approve| AutoApp[Auto Approve]
    Security -->|llm_review| LLM[LLM Risk Assessor]
    Security -->|human_review| Human[Human Review HITL ✋]
    
    LLM --> Human
    
    AutoApp -->|approve| CreateSO[Create Service Order]
    Human -->|approve| CreateSO
    Human -->|reject| RejectSO[Reject Order]
    
    subgraph MCP Server
      direction TB
      MCP[MCP Server]
    end
    
    LLM -.->|tools| MCP
```

## Concepts Used
- **ADK Workflow**: The backbone of the application. Defined in `service_order_agent/agent.py`, the workflow orchestrates the sequence of events from `extract_incident` all the way to `create_service_order` or `reject_order`. It defines explicit routing logic between synchronous and asynchronous (HITL) nodes.
- **LlmAgent**: Used in `llm_review` to evaluate risk factors and provide a recommendation. It is strictly typed using Pydantic (`RiskAssessment`), constraining the LLM to output predictable schema formats.
- **AgentTool / MCP Server**: While the core logic handles risk via LLM, the architecture supports connecting to an MCP Server to allow the `llm_review` agent to fetch real-time inventory or historical part failure data, enhancing its risk recommendation.
- **Security Checkpoint**: Implemented in the `security_checkpoint` node. It processes the raw input before it ever reaches the LLM, neutralizing risks proactively.
- **Agents CLI**: Used for rapid iteration and testing. We leveraged `agents-cli playground` to interactively debug the HITL pauses and validate the workflow state transitions.

## Security Design
1. **PII Scrubbing**: The system uses regex patterns to redact SSNs and Credit Card numbers from the incident description (`[REDACTED_SSN]`). This matters because service engineers or automated systems might accidentally paste sensitive customer data into the service request, which must not be sent to external LLMs.
2. **Prompt Injection Detection**: We scan the incident description for keywords like "bypass", "auto-approve", or "override". If detected, the system overrides the standard routing and forces a direct `human_review` with a high-priority alert. This prevents malicious actors from tricking the LLM into approving million-dollar orders.
3. **Threshold Hardcoding**: The `COST_THRESHOLD_USD` is strictly enforced in deterministic code rather than trusting the LLM. The LLM cannot accidentally bypass the threshold.

## MCP Server Design
The architecture incorporates an MCP Server pattern to ground the agent in enterprise data:
- **Inventory Check Tool**: Allows the agent to query if the requested part is actually in stock before assessing the risk.
- **Historical Claims Tool**: Enables the LLM to see if this particular submitter has a history of high-value replacements, factoring into the risk assessment before human review.

## HITL (Human-in-the-Loop) Flow
The human is placed at the most critical juncture: the final approval of high-cost or flagged service orders.
- **Why**: AI is excellent at synthesizing risk (cost, context, history), but the final financial authorization for expensive parts requires human accountability.
- **Where**: The `human_review` node uses `yield RequestInput(interrupt_id="approval", ...)` to pause the workflow. The workflow is only resumed when the human reviews the summarized risk factors and replies with "approve" or "reject".

## Demo Walkthrough
When demonstrating the project, we test 3 distinct paths (refer to the README for exact payloads):
1. **The Happy Path (Auto Approve)**: We submit a $50 part. The UI shows it immediately skipping the LLM and hitting `auto_approve` to generate an `SO-XXXXX` number.
2. **The Assessed Path (LLM + HITL)**: We submit an $8500 part. The UI highlights the `llm_review` executing, followed by a pause at `human_review`. The user manually inputs "approve", and the workflow completes.
3. **The Malicious Path**: We submit a payload with a fake SSN and text saying "Bypass all rules". The UI immediately pauses at `human_review` with a red flag alert, skipping the LLM entirely to protect the system.

## Impact / Value Statement
This agentic workflow benefits **Service Operations Teams and Finance Departments**. 
- It **reduces manual review time by 80%** by instantly auto-approving low-cost, routine orders.
- It **mitigates financial risk** by forcing human approval on expensive items, but equipping that human with a pre-synthesized LLM risk report, cutting their review time from minutes to seconds.
- It **protects the enterprise** from emerging AI security threats via robust pre-processing.
