"""Human-readable device labels ("Chrome on Windows") for the sessions list.

A deliberately small parser: the label is for people recognising their own devices, never
for security decisions, so approximate is fine and no extra dependency is needed.
"""

import re

# Order matters: Edge and Opera also contain "Chrome", and Chrome also contains "Safari".
_BROWSERS = (
    ("Edge", re.compile(r"Edg(e|A|iOS)?/")),
    ("Opera", re.compile(r"OPR/|Opera")),
    ("Samsung Internet", re.compile(r"SamsungBrowser/")),
    ("Firefox", re.compile(r"Firefox/|FxiOS/")),
    ("Chrome", re.compile(r"Chrome/|CriOS/")),
    ("Safari", re.compile(r"Safari/")),
)
_SYSTEMS = (
    ("Android", re.compile(r"Android")),
    ("iOS", re.compile(r"iPhone|iPad|iPod")),
    ("Windows", re.compile(r"Windows")),
    ("macOS", re.compile(r"Macintosh|Mac OS X")),
    ("ChromeOS", re.compile(r"CrOS")),
    ("Linux", re.compile(r"Linux")),
)


def describe_device(user_agent: str) -> str:
    browser = next((name for name, pattern in _BROWSERS if pattern.search(user_agent)), None)
    system = next((name for name, pattern in _SYSTEMS if pattern.search(user_agent)), None)
    if browser and system:
        return f"{browser} on {system}"
    return browser or system or "Unknown device"
