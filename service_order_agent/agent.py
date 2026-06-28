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
    # Handle the case where ADK passes a resume function_response to the START node
    # Use extremely robust duck typing and string representation checking
    node_str = str(node_input)
    is_content_obj = type(node_input).__name__ == "Content" or "Content(" in node_str
    
    if is_content_obj:
        if "function_response=" in node_str or "function_call=" in node_str:
            if "approve" in node_str.lower() and "reject" not in node_str.lower():
                ctx.state["auto_decision"] = "approve"
            elif "reject" in node_str.lower():
                ctx.state["auto_decision"] = "reject"
                
            if "incident" in ctx.state:
                return Event(output=IncidentDetails.model_validate(ctx.state["incident"]))
            else:
                raise ValueError("Received function response but no incident in state.")
                
        parts = getattr(node_input, "parts", []) if hasattr(node_input, "parts") else []
        if parts:
            part = parts[0]
            text_val = getattr(part, "text", None)
            if text_val:
                try:
                    data = json.loads(text_val)
                    node_input = data
                except Exception:
                    # If it's a resume but sent as text instead of function response
                    if "incident" in ctx.state and text_val.strip().lower() in ["approve", "reject"]:
                        ctx.state["auto_decision"] = text_val.strip().lower()
                        return Event(output=IncidentDetails.model_validate(ctx.state["incident"]))
                    raise ValueError(f"Expected JSON input for incident extraction, got: {text_val}")
            else:
                raise ValueError("Content input lacks text or function_response.")
        else:
            # Fallback if we couldn't parse parts
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

    # Extreme fallback: if data is somehow STILL a Content object, DO NOT PASS to Pydantic!
    if type(data).__name__ == "Content" or "Content(" in str(data):
        if "incident" in ctx.state:
            return Event(output=IncidentDetails.model_validate(ctx.state["incident"]))
        else:
            raise ValueError("Received Content object but no incident in state.")
            
    # Parse and validate using Pydantic schema
    incident = IncidentDetails.model_validate(data)
    
    # Save the incident in state so it's accessible by downstream nodes
    ctx.state["incident"] = incident.model_dump()
    
    # Reset prompted flag for new invocations to ensure human_review pauses
    prompted_key = f"prompted_{incident.incident_number}"
    if prompted_key in ctx.state:
        del ctx.state[prompted_key]
        
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
    incident_num = ctx.state["incident"]["incident_number"]
    prompted_key = f"prompted_{incident_num}"
    
    # Check if we have an auto_decision from a resumed START node
    if "auto_decision" in ctx.state:
        decision = ctx.state["auto_decision"]
        del ctx.state["auto_decision"]
        
        if decision == "approve":
            return Event(output={"decision": "approved", "incident": ctx.state["incident"]}, route="approve")
        else:
            return Event(output={"decision": "rejected", "incident": ctx.state["incident"]}, route="reject")

    if not ctx.state.get(prompted_key):
        ctx.state[prompted_key] = True
        msg = (f"Please review this service order.\n"
               f"Risk factors: {node_input.risk_factors}\n"
               f"Recommendation: {node_input.recommendation}\n"
               f"Reply with 'approve' or 'reject'.")
        return RequestInput(interrupt_id="approval", message=msg)
        
    approval_input = ctx.resume_inputs.get("approval", "")
    
    # Safeguard: if user pasted a new JSON payload instead of replying
    approval_str = str(approval_input).strip()
    if "incident_number" in approval_str and "{" in approval_str:
        msg = (f"Error: Expected 'approve' or 'reject', but received a JSON payload.\n"
               f"Please type 'approve' or 'reject' to finish the current order.\n"
               f"To start a new test case, please reset the playground session first.")
        return RequestInput(interrupt_id="approval", message=msg)
    
    if hasattr(approval_input, "parts") and getattr(approval_input, "parts", None):
        decision = approval_input.parts[0].text.strip().lower()
    elif isinstance(approval_input, dict):
        # The UI or CLI might pass a dictionary like {"approval": "approve"}
        # If it's a dict and has the 'approval' key, extract that.
        val = approval_input.get("approval")
        if val is None:
            # Fallback to the first value in the dict
            val = list(approval_input.values())[0] if approval_input else ""
        decision = str(val).strip().lower()
    else:
        decision = str(approval_input).strip().lower()
        
    # Extra safety: check if the string contains the keyword rather than strict equality
    if "approve" in decision and "reject" not in decision:
        decision = "approve"
    elif "reject" in decision:
        decision = "reject"
    
    if decision == "approve":
        return Event(output={"decision": "approved", "incident": ctx.state["incident"]}, route="approve")
    else:
        return Event(output={"decision": "rejected", "incident": ctx.state["incident"]}, route="reject")


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
