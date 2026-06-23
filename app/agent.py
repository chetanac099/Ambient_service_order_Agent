import os
from dotenv import load_dotenv
load_dotenv()

from google.adk.apps import App
from service_order_agent.agent import root_agent

# Use Vertex AI only if GOOGLE_CLOUD_PROJECT is explicitly set in env
if os.environ.get("GOOGLE_CLOUD_PROJECT"):
    os.environ["GOOGLE_CLOUD_LOCATION"] = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"
else:
    # Otherwise it uses AI Studio via GEMINI_API_KEY from .env
    pass

app = App(
    root_agent=root_agent,
    name="app",
)
