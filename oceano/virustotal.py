"""VirusTotal v3 helpers — a keyless hash reputation lookup (just a GUI link) and an API-key file
upload. Used by the email reader's attachment context menu (right-click → check/upload). The API key
lives in web.json (Settings); callers pass it in, so this module never touches the settings store.
"""
import requests

GUI = "https://www.virustotal.com/gui"
API = "https://www.virustotal.com/api/v3"
_TIMEOUT = 60
_MAX_DIRECT = 32 * 1024 * 1024   # VT's direct-upload limit (bigger needs a special upload URL)


def file_report_url(sha256):
    """The VirusTotal web report for a file hash — open it to see detections. No API key needed:
    if VT has seen the file before, this shows the verdict; otherwise it offers to scan."""
    return f"{GUI}/file/{sha256}"


def analysis_url(analysis_id):
    """Web page for a just-submitted upload's analysis."""
    return f"{GUI}/file-analysis/{analysis_id}"


def upload(api_key, data, filename="attachment"):
    """Upload bytes to VirusTotal for scanning. Returns {ok, id, url} or {ok: False, error}."""
    if not api_key:
        return {"ok": False, "error": "no VirusTotal API key set (Settings → Mail)"}
    if not data:
        return {"ok": False, "error": "empty file"}
    if len(data) > _MAX_DIRECT:
        return {"ok": False, "error": "file too large for direct upload (32 MB max)"}
    try:
        r = requests.post(f"{API}/files", headers={"x-apikey": api_key},
                          files={"file": (filename, data)}, timeout=_TIMEOUT)
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "error": f"upload failed: {e}"}
    if r.status_code == 401:
        return {"ok": False, "error": "VirusTotal rejected the API key (401)"}
    if r.status_code == 429:
        return {"ok": False, "error": "VirusTotal rate limit hit (free keys allow ~4 req/min)"}
    if r.status_code >= 400:
        return {"ok": False, "error": f"VirusTotal error {r.status_code}: {r.text[:200]}"}
    try:
        aid = r.json()["data"]["id"]
    except Exception:                                         # noqa: BLE001
        return {"ok": False, "error": "unexpected VirusTotal response"}
    return {"ok": True, "id": aid, "url": analysis_url(aid)}
