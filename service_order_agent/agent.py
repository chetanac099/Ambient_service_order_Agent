import base64
import json
import random
import re
from typing import Any

from google.adk.workflow import Workflow, node
from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.genai import types

from .config import COST_THRESHOLD_USD, LLM_MODEL
from .schemas import IncidentDetails, RiskAssessment, ServiceOrderResult


@node
def extract_incident(ctx: Context, node_input: Any) -> Event:
    """Parses JSON event (supports base64 decoded 'data' key or plain dict)."""
    data = node_input
    
    if isinstance(node_input, types.Content) and node_input.parts:
        try:
            text_val = node_input.parts[0].text
            data = json.loads(text_val)
            node_input = data
        except Exception:
            pass
            
    if isinstance(node_input, dict) and 'data' in node_input:
        raw_data = node_input['data']
        if isinstance(raw_data, str):
            try:
                # Try base64 decoding (e.g. Pub/Sub)
                decoded = base64.b64decode(raw_data).decode('utf-8')
                data = json.loads(decoded)
            except Exception:
                # Fallback to plain JSON string parsing
                try:
                    data = json.loads(raw_data)
                except Exception:
                    # If it's a plain string that can't be parsed
                    pass
        elif isinstance(raw_data, dict):
            data = raw_data
    elif isinstance(node_input, str):
        try:
            data = json.loads(node_input)
        except Exception:
            pass

    # Parse and validate using Pydantic schema
    incident = IncidentDetails.model_validate(data)
    
    # Save the incident in state so it's accessible by downstream nodes
    ctx.state["incident"] = incident.model_dump()

    return Event(output=incident)


@node
def security_checkpoint(ctx: Context, node_input: IncidentDetails) -> Event:
    """Scrubs PII and checks for prompt injection."""
    desc = node_input.description
    redacted = []

    # Scrub SSN
    ssn_pattern = r"\b\d{3}-\d{2}-\d{4}\b"
    if re.search(ssn_pattern, desc):
        desc = re.sub(ssn_pattern, "[REDACTED_SSN]", desc)
        redacted.append("SSN")

    # Scrub CC
    cc_pattern = r"\b(?:\d[ -]*?){13,16}\b"
    if re.search(cc_pattern, desc):
        desc = re.sub(cc_pattern, "[REDACTED_CC]", desc)
        redacted.append("CREDIT_CARD")

    node_input.description = desc
    ctx.state["incident"]["description"] = desc
    ctx.state["redacted_categories"] = redacted

    # Prompt injection check
    desc_lower = desc.lower()
    injection_keywords = ["auto-approve", "auto approve", "bypass", "ignore", "override", "force"]
    if any(kw in desc_lower for kw in injection_keywords):
        # Security event detected, bypass LLM and go straight to human review
        assessment = RiskAssessment(
            risk_factors="Security Event: Potential prompt injection or rule bypass detected in the description.",
            recommendation="Reject"
        )
        return Event(output=assessment, route="human_review")

    # Clean records: Route based on threshold
    if node_input.part_replacement_cost < COST_THRESHOLD_USD:
        return Event(output=node_input, route="auto_approve")
    else:
        return Event(output=node_input, route="llm_review")


llm_review = LlmAgent(
    name="llm_review",
    model=LLM_MODEL,
    instruction=(
        "You are a risk assessor for service orders. Review the following incident details. "
        "Assess the risk factors associated with this order and provide a recommendation for approval or rejection."
    ),
    output_schema=RiskAssessment,
)


@node(rerun_on_resume=True)
async def human_review(ctx: Context, node_input: RiskAssessment) -> RequestInput | Event:
    """Pauses workflow to request human review based on risk assessment."""
    if not ctx.resume_inputs:
        msg = (f"Please review this service order.\n"
               f"Risk factors: {node_input.risk_factors}\n"
               f"Recommendation: {node_input.recommendation}\n"
               f"Reply with 'approve' or 'reject'.")
        yield RequestInput(interrupt_id="approval", message=msg)
        return
        
    decision = str(ctx.resume_inputs.get("approval", "")).strip().lower()
    
    if decision == "approve":
        yield Event(output={"decision": "approved", "incident": ctx.state["incident"]}, route="approve")
    else:
        yield Event(output={"decision": "rejected", "incident": ctx.state["incident"]}, route="reject")


@node
def auto_approve(ctx: Context, node_input: IncidentDetails) -> Event:
    """Directly output an approved state."""
    return Event(output={"decision": "auto-approved", "incident": ctx.state["incident"]}, route="approve")


@node
def create_service_order(ctx: Context, node_input: dict) -> Event:
    """Generates a service order for approved incidents."""
    incident_num = node_input["incident"]["incident_number"]
    decision = node_input["decision"]
    
    # Generate mock service order number
    so_number = f"SO-{random.randint(10000, 99999)}"
    
    result = ServiceOrderResult(
        service_order=so_number,
        outcome=decision,
        incident_number=incident_num
    )
    
    return Event(output=result)


@node
def reject_order(ctx: Context, node_input: dict) -> Event:
    """Records a rejected order without creating a service order number."""
    incident_num = node_input["incident"]["incident_number"]
    decision = node_input["decision"]
    
    result = ServiceOrderResult(
        service_order=None,
        outcome=decision,
        incident_number=incident_num
    )
    
    return Event(output=result)


# Define the Workflow graph
root_agent = Workflow(
    name="service_order_approval_workflow",
    edges=[
        ('START', extract_incident),
        (extract_incident, security_checkpoint),
        (security_checkpoint, {
            "auto_approve": auto_approve,
            "llm_review": llm_review,
            "human_review": human_review
        }),
        (llm_review, human_review),
        (human_review, {
            "approve": create_service_order,
            "reject": reject_order
        }),
        (auto_approve, {
            "approve": create_service_order
        })
    ],
    output_schema=ServiceOrderResult
)
