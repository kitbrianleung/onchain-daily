import os, base64, requests
from nacl import encoding, public

TOKEN = os.environ["IG_ACCESS_TOKEN"]
GH_PAT = os.environ["GH_PAT"]
REPO = os.environ["GITHUB_REPOSITORY"]

r = requests.get("https://graph.instagram.com/refresh_access_token",
                 params={"grant_type": "ig_refresh_token", "access_token": TOKEN}, timeout=30)
r.raise_for_status()
new_token = r.json()["access_token"]

h = {"Authorization": f"token {GH_PAT}", "Accept": "application/vnd.github+json"}
k = requests.get(f"https://api.github.com/repos/{REPO}/actions/secrets/public-key", headers=h).json()
box = public.SealedBox(public.PublicKey(k["key"].encode(), encoding.Base64Encoder()))
enc = base64.b64encode(box.encrypt(new_token.encode())).decode()
resp = requests.put(f"https://api.github.com/repos/{REPO}/actions/secrets/IG_ACCESS_TOKEN",
                    headers=h, json={"encrypted_value": enc, "key_id": k["key_id"]})
resp.raise_for_status()
print("IG_ACCESS_TOKEN refreshed and updated in repo secrets")
