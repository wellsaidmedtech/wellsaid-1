import os
import httpx
import base64

# Reads `HUME_API_KEY` and `HUME_SECRET_KEY` from environment variables
HUME_API_KEY = os.getenv('HUME_API_KEY')
HUME_SECRET_KEY = os.getenv('HUME_SECRET_KEY');

auth = f"{HUME_API_KEY}:{HUME_SECRET_KEY}"
encoded_auth = base64.b64encode(auth.encode()).decode()
resp = httpx.request(
    method="POST",
    url="https://api.hume.ai/oauth2-cc/token",
    headers={"Authorization": f"Basic {encoded_auth}"},
    data={"grant_type": "client_credentials"},
)

access_token = resp.json()['access_token']
