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
    """Navigate the shared browser and return the rendered text."""
    refusal = safety.check_url(url)
    if refusal:
        return refusal
    return livebrowser.navigate(url, read=True)


def screenshot(url, name="screenshot.png"):
    """Navigate the shared browser to a URL and save a full-page screenshot."""
    refusal = safety.check_url(url)
    if refusal:
        return refusal
    if not name.lower().endswith((".png", ".jpg", ".jpeg")):
        name += ".png"
    path = config.WORKSPACE / name
    livebrowser.navigate(url)
    livebrowser.save_screenshot(path)
    return f"saved screenshot of {url} to {path.relative_to(config.WORKSPACE)}"
