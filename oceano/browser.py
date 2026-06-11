"""Browser tool surface — delegates to the persistent shared session (livebrowser),
so the agent's browsing happens on the very same page the user sees and can drive.

Every entry point here runs safety.check_url first, so the SSRF guard (no
loopback/private/link-local/metadata targets) holds no matter which caller
navigates — callers don't have to remember to check.
"""
import config
from oceano import livebrowser, safety

LATEST = livebrowser.LATEST          # shared frame buffer (mutated in place by the worker)


def open_url(url):
    """Open a URL in the shared browser and return the rendered text. While the
    agent is researching (after a web_search) this lands in a NEW tab; otherwise it
    reuses the active tab."""
    refusal = safety.check_url(url)
    if refusal:
        return refusal
    return livebrowser.open(url, read=True)


def screenshot(url, name="screenshot.png", shared=True):
    """Save a full-page screenshot of a URL into the workspace and return a markdown
    image reference (so it renders in the web chat / is delivered as a Telegram photo).

    shared=True  → drive the shared live browser (web UI: the user watches it happen).
    shared=False → a throwaway headless capture that doesn't disturb the shared view
                   (off-web channels like Telegram)."""
    refusal = safety.check_url(url)
    if refusal:
        return refusal
    if not name.lower().endswith((".png", ".jpg", ".jpeg")):
        name += ".png"
    path = config.WORKSPACE / name
    if shared:
        livebrowser.navigate(url)
        livebrowser.save_screenshot(path)
    else:
        try:
            livebrowser.capture(url, path)
        except Exception as e:
            return f"could not capture {url}: {type(e).__name__}: {e}"
    rel = path.relative_to(config.WORKSPACE)
    return f"saved screenshot of {url} to {rel}\n\n![screenshot of {url}]({rel})"
