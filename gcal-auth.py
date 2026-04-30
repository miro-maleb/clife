#!/usr/bin/env python3
"""
One-time Google Calendar auth for headless/phone environments.
Prints a URL to open in browser, then saves credentials after you paste the code.
"""
import pickle
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar"]
CLIENT_SECRETS = str(Path.home() / ".config" / "gcalcli" / "client_secret.json")
OAUTH_PATH = Path.home() / ".local" / "share" / "gcalcli" / "oauth"

flow = InstalledAppFlow.from_client_secrets_file(
    CLIENT_SECRETS, SCOPES,
    redirect_uri="urn:ietf:wg:oauth:2.0:oob",
)
auth_url, _ = flow.authorization_url(prompt="consent")
print("\nOpen this URL in your browser:\n")
print(auth_url)
code = input("\nPaste the authorization code: ").strip()
flow.fetch_token(code=code)
creds = flow.credentials

OAUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
with open(OAUTH_PATH, "wb") as f:
    pickle.dump(creds, f)

print(f"\nCredentials saved to {OAUTH_PATH}")
