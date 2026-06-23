import base64
import json
import logging
from fastapi import FastAPI, Request, BackgroundTasks
from google.adk.runners import InMemoryRunner
from google.genai import types

from app.agent import app as agent_app
from service_order_agent.schemas import IncidentDetails, RiskAssessment

# Configure standard Python logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("ambient_service")

app = FastAPI()

# Note: Telemetry otel_to_cloud=False is the default unless explicitly configured via ADK.
# We are intentionally relying on standard python logging here.

async def process_event(app_name: str, payload: dict):
    logger.info(f"Starting workflow for session/app_name: {app_name}")
    from google.adk.cli.utils.local_storage import create_local_session_service, create_local_artifact_service
    runner = InMemoryRunner(app=agent_app)
    
    # Patch the runner to persist to the same local DB that the Playground UI reads
    runner.session_service = create_local_session_service(base_dir="app")
    runner.artifact_service = create_local_artifact_service(base_dir="app")
    
    # We use the normalized subscription name as the user_id for session correlation
    session = await runner.session_service.create_session(
        app_name="app", user_id=app_name
    )
    
    json_str = json.dumps(payload)
    
    try:
        async for event in runner.run_async(
            user_id=app_name,
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part.from_text(text=json_str)]),
        ):
            if getattr(event, 'output', None) is not None:
                if isinstance(event.output, IncidentDetails):
                    logger.info(f"[Security Checkpoint] Clean Incident Output: {event.output}")
                elif isinstance(event.output, RiskAssessment):
                    logger.info(f"[LLM/Security Review] Assessment: {event.output}")
                else:
                    logger.info(f"[Workflow Output] {event.output}")
            
            # Note: We won't handle RequestInput interrupts here automatically,
            # but they will pause the workflow and wait for a human in the UI.
    except Exception as e:
        logger.error(f"Error executing workflow: {e}")

@app.post("/pubsub")
async def handle_pubsub(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()
    
    message = body.get("message", {})
    subscription = body.get("subscription", "unknown-sub")
    
    # Normalize subscription path down to short name
    # e.g., projects/my-project/subscriptions/my-sub -> my-sub
    sub_name = subscription.split("/")[-1]
    
    data_b64 = message.get("data")
    if not data_b64:
        logger.warning("Received Pub/Sub message without data")
        return {"status": "ok", "detail": "no data"}
        
    try:
        data_json = base64.b64decode(data_b64).decode("utf-8")
        payload = json.loads(data_json)
    except Exception as e:
        logger.error(f"Failed to decode message data: {e}")
        return {"status": "error", "detail": "invalid data encoding"}
        
    logger.info(f"Received ambient trigger from subscription '{sub_name}'. Payload: {payload}")
    
    # Run the workflow in the background so Pub/Sub gets an immediate 200 OK ACK
    background_tasks.add_task(process_event, sub_name, payload)
    
    return {"status": "ok"}
