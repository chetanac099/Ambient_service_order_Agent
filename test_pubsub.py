import requests
import base64
import json

payload = {
    "Incident Number": "INC23568",
    "Part Number": "PROD Nozzle",
    "submitter": "alice@company.com",
    "category": "Drilling",
    "description": "Nozzle for drilling",
    "date": "2026-06-06",
    "Part Replacement Cost": 120
}

data_b64 = base64.b64encode(json.dumps(payload).encode()).decode()

pubsub_message = {
    "message": {
        "data": data_b64,
        "messageId": "12345"
    },
    "subscription": "projects/my-project/subscriptions/ambient-test-session"
}

resp = requests.post("http://127.0.0.1:8081/pubsub", json=pubsub_message)
print("Response:", resp.json())
