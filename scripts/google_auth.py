"""One-time Google login (Calendar, Gmail read/drafts, Drive read-only). Opens a browser window and saves a token file.

1. In Google Cloud Console create an OAuth client of type "Desktop app" and enable the Calendar, Gmail and Drive APIs.
2. Download its JSON to the path in GOOGLE_CLIENT_SECRET_PATH (default secrets/google_client.json).
3. Run:  python -m scripts.google_auth
"""

from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

from app.config import settings
from app.tools.google_common import SCOPES

flow = InstalledAppFlow.from_client_secrets_file(settings.google_client_secret_path, SCOPES)
credentials = flow.run_local_server(port=0)
Path(settings.google_token_path).write_text(credentials.to_json())
print(f"Saved {settings.google_token_path}")
