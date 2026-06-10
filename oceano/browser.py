"""Browser tool surface — delegates to the persistent shared session (livebrowser),
so the agent's browsing happens on the very same page the user sees and can drive.
"""
import config
from oceano import livebrowser

LATEST = livebrowser.LATEST          # shared frame buffer (mutated in place by the worker)


def open_url(url):
    """Navigate the shared browser and return the rendered text."""
    return livebrowser.navigate(url, read=True)


def screenshot(url, name="screenshot.png"):
    """Navigate the shared browser to a URL and save a full-page screenshot."""
    if not name.lower().endswith((".png", ".jpg", ".jpeg")):
        name += ".png"
    path = config.WORKSPACE / name
    livebrowser.navigate(url)
    livebrowser.save_screenshot(path)
    return f"saved screenshot of {url} to {path.relative_to(config.WORKSPACE)}"
