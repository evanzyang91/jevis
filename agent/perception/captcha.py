"""Detect human-verification pages, and where a run goes once one is passed.

Purely observational: given what the page shows, decide whether it is asking
a person to prove they are not a bot. The supervisor uses this to pause the
run, hand the browser to the user, and pick up again once the check is gone.

Kept as a small set of URL, title and text signals rather than model-driven,
because this decision runs every step and must never be its own failure mode.
A false positive freezes a run on a normal page, so every signal here is one
that a shopping, food or reference page would not carry:

- URL and title signals match whole path segments, query keys and exact
  titles, never substrings (Walmart sells "Challenge" butter).
- Text signals come in two strengths. Strong ones are sentences only a
  verification page writes. Weak ones ("press and hold", "verify your
  identity") also appear on normal pages (a product manual, an account
  page), so they count only on a near-empty page, which is what every
  verification page is.
- Generic words ("captcha", "Cloudflare", "access denied") are not signals:
  forms print "protected by reCAPTCHA", articles mention Cloudflare, and an
  access-denied page is a block no person can clear.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .observation import Observation

# A verification page has almost nothing to press: Walmart's block page reads
# as 0 controls, Cloudflare's as 2, Amazon's captcha form as about 5.
_SPARSE_CONTROLS = 6

# Whole leading path segments that only a verification page uses.
_PATH_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("blocked",),                    # Walmart (PerimeterX): /blocked?url=<base64 path>
    ("sorry", "index"),              # Google: /sorry/index?continue=<url>
    ("errors", "validatecaptcha"),   # Amazon: /errors/validateCaptcha
    ("px-captcha",),                 # PerimeterX hosted challenge
    ("distil_r_captcha.html",),      # Distil
    ("cdn-cgi", "challenge-platform"),  # Cloudflare
)

# Query keys Cloudflare adds to the URL while its challenge is running.
_QUERY_PREFIXES = ("__cf_chl_",)

# Exact page titles (lowercased, trailing dots and spaces dropped).
_TITLES = frozenset({
    "just a moment",                        # Cloudflare
    "attention required! | cloudflare",     # Cloudflare, older
    "access to this page has been denied",  # PerimeterX
    "robot or human?",                      # Walmart US (PerimeterX)
    "pardon our interruption",              # Imperva / Distil
    "human verification",
})

# Sentences only a verification page writes. Matched at any page size.
_STRONG_TEXT = (
    "unusual traffic from your computer network",                 # Google
    "enter the characters you see below",                         # Amazon
    "type the characters you see in this image",                  # Amazon
    "sorry, we just need to make sure you're not a robot",        # Amazon
    "we like real shoppers, not robots",                          # Walmart CA (PerimeterX)
    "activate and hold the button to confirm that you're human",  # Walmart US (PerimeterX)
    "press & hold to confirm you are a human",                    # PerimeterX
    "press and hold to confirm you are a human",                  # PerimeterX
    "verify you are human by completing the action below",        # Cloudflare Turnstile
    "needs to review the security of your connection",            # Cloudflare
    "while the website verifies you are not a bot",               # Cloudflare
    "checking if the site connection is secure",                  # Cloudflare, older
    "checking your browser before accessing",                     # Cloudflare, older
    "please complete the security check to access",               # Cloudflare, older
    "made us think you were a bot",                               # Imperva / Distil
)

# Phrases a normal page can also carry. Matched only on a near-empty page.
_WEAK_TEXT = (
    "press & hold",
    "press and hold",
    "verify you are human",
    "verify you are a human",
    "verify you're human",
    "verify that you are human",
    "confirm you are not a robot",
    "confirm you're not a robot",
    "are you a robot",
    "not a robot",
    "performing security verification",
    "complete the security check",
    "verify your identity",
    "solve this captcha",
    "checking your browser",
)


@dataclass(frozen=True, slots=True)
class CaptchaSignal:
    """Non-None when the page looks like a human check.

    `reason` is one plain sentence for the UI and the run log; `match` is the
    signal that fired, for tests and the developer view; `source` says where
    it was found ("url", "title" or "text")."""

    reason: str
    match: str
    source: str = "text"


@dataclass(frozen=True, slots=True)
class PageProbe:
    """A cheap look at the page while a run waits on a person.

    Read without running anything the page can see (see `probe`). `digest`
    changes whenever the document does, so a waiting run knows when it is
    worth reading the page properly again."""

    url: str
    title: str
    digest: str


def _host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _reason(url: str) -> str:
    host = _host(url)
    return f"{host or 'The site'} wants a person to confirm they are human."


def _normalise(text: str) -> str:
    """Lowercase, straight apostrophes, single spaces."""
    return " ".join(text.lower().replace("’", "'").replace("‘", "'").split())


def _segments(path: str) -> list[str]:
    return [segment for segment in path.lower().split("/") if segment]


def url_signal(url: str) -> str | None:
    """The URL signal that marks this as a verification page, or None."""
    parsed = urlparse(url)
    segments = _segments(parsed.path)
    for prefix in _PATH_PREFIXES:
        if tuple(segments[: len(prefix)]) == prefix:
            return "/" + "/".join(prefix)
    for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower().startswith(_QUERY_PREFIXES):
            return key
    return None


def title_signal(title: str) -> str | None:
    """The title that marks this as a verification page, or None."""
    clean = _normalise(title).rstrip(". …")
    return clean if clean in _TITLES else None


def detect_page(url: str, title: str, text: str, controls: int | None) -> CaptchaSignal | None:
    """Whether a page with this URL, title and visible text is a human check.

    `controls` is how many interactive elements the page shows; None when the
    caller does not know, and then the weak text signals are not used."""
    match = url_signal(url)
    if match is not None:
        return CaptchaSignal(reason=_reason(url), match=match, source="url")
    match = title_signal(title)
    if match is not None:
        return CaptchaSignal(reason=_reason(url), match=match, source="title")
    haystack = _normalise(f"{title}\n{text}")
    for phrase in _STRONG_TEXT:
        if phrase in haystack:
            return CaptchaSignal(reason=_reason(url), match=phrase, source="text")
    if controls is not None and controls <= _SPARSE_CONTROLS:
        for phrase in _WEAK_TEXT:
            if phrase in haystack:
                return CaptchaSignal(reason=_reason(url), match=phrase, source="text")
    return None


def detect(observation: Observation) -> CaptchaSignal | None:
    """Return a signal when the observed page looks like a bot check, or None."""
    return detect_page(observation.url, observation.title, observation.text, len(observation.elements))


def _decode_path(raw: str) -> str | None:
    """A base64 site path ("L2Vu" is "/en"), or None when it is not one."""
    raw = raw.strip().replace(" ", "+")  # an unescaped "+" arrives from the query string as a space
    padded = raw + "=" * (-len(raw) % 4)
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            path = decoder(padded).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            continue
        if path.startswith("/") and not path.startswith("//") and path.isprintable():
            return path
    return None


def intended_url(url: str) -> str | None:
    """Where the visitor was going when the site showed this check instead.

    Verification pages carry the destination: Walmart as a base64 path
    (`/blocked?url=L2Vu` was `/en`), Google as `continue=`, Amazon as
    `amzn-r=`, and Cloudflare as the page URL itself plus its own challenge
    keys. None when this URL names no destination, or names one on another
    site (the run never follows a check to a different site).
    """
    parsed = urlparse(url)
    if not parsed.scheme.startswith("http") or not parsed.netloc:
        return None
    segments = _segments(parsed.path)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    values = {key.lower(): value for key, value in query}

    def same_site(path: str) -> str:
        return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))

    if segments[:1] == ["blocked"] and values.get("url"):
        path = _decode_path(values["url"])
        return same_site(path) if path else None
    if segments[:2] == ["sorry", "index"] and values.get("continue"):
        target = urlparse(values["continue"])
        if target.scheme in {"http", "https"} and _host(values["continue"]) == _host(url):
            return values["continue"]
        return None
    if segments[:2] == ["errors", "validatecaptcha"] and values.get("amzn-r"):
        path = values["amzn-r"]
        return same_site(path) if path.startswith("/") and not path.startswith("//") else None
    kept = [(key, value) for key, value in query if not key.lower().startswith(_QUERY_PREFIXES)]
    if len(kept) != len(query):
        return urlunparse(parsed._replace(query=urlencode(kept)))
    return None


async def probe(page: Any) -> PageProbe:
    """Look at the page without running anything it can see.

    The URL comes from Playwright's own navigation tracking, and the title and
    document from Playwright's isolated utility world. No script runs in the
    page's world, no DOM node is marked, and no event is dispatched, so a
    verification widget a person is working on sees nothing from the agent.
    (Locator calls are avoided on purpose: they dispatch marker events into
    the page.)"""
    url = page.url
    title = await page.title()
    document = await page.content()
    return PageProbe(url=url, title=title, digest=hashlib.sha1(document.encode("utf-8", "replace")).hexdigest())
