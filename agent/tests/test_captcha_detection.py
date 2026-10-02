"""Captcha detection: real verification pages are caught, normal pages are not.

The positive cases are the pages runs have actually met (Walmart's PerimeterX
block page, DoorDash behind Cloudflare) plus the published shapes of Amazon's,
Google's and PerimeterX's own pages. The negative cases are normal pages that
carry the same words; a false positive would freeze a run on a working page.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone

import pytest

from agent.perception import Element, Observation, Rect, detect_captcha
from agent.perception.captcha import detect_page, intended_url, probe


def _obs(*, url: str = "https://example.com/", title: str = "", text: str = "", controls: int = 0) -> Observation:
    elements = tuple(
        Element(ref=f'[data-agent-ref="e{i}"]', role="link", name=f"Link {i}", bounds=Rect(0, 0, 10, 10))
        for i in range(controls)
    )
    return Observation(
        url=url,
        title=title,
        text=text,
        elements=elements,
        marker="m",
        fingerprint="fp",
        guards={},
        can_go_back=False,
        can_scroll_up=False,
        can_scroll_down=False,
        viewport=(1280, 800),
        captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


# ---- Verification pages are caught ------------------------------------------


def test_walmart_ca_block_page_from_the_pasta_run() -> None:
    """The exact first observation of the run that paused on walmart.ca."""
    signal = detect_captcha(_obs(
        url=("https://www.walmart.ca/blocked?url=L2Vu&uuid=c302fdc0-bdf3-11f1-837f-afb37b2e4e57"
             "&vid=f9559fb4-b4cf-11f1-8a24-561427041269&g=b"),
        title="Verify Your Identity",
        text=("We like real shoppers, not robots! Please press and hold the button below to verify "
              "yourself so we can keep spam bots off of Walmart.ca."),
    ))
    assert signal is not None
    assert signal.source == "url"
    assert signal.match == "/blocked"
    assert "walmart.ca" in signal.reason


def test_walmart_ca_block_text_alone_is_enough() -> None:
    signal = detect_captcha(_obs(url="https://www.walmart.ca/en", text="We like real shoppers, not robots!"))
    assert signal is not None and signal.source == "text"


def test_walmart_us_robot_or_human() -> None:
    signal = detect_captcha(_obs(
        url="https://www.walmart.com/blocked?url=L3NlYXJjaD9xPXBhc3Rh&uuid=x&vid=y&g=b",
        title="Robot or human?",
        text="Activate and hold the button to confirm that you’re human. Thank You!",
    ))
    assert signal is not None
    # Without the URL, the title and the curly-apostrophe sentence still catch it.
    assert detect_captcha(_obs(url="https://www.walmart.com/", title="Robot or human?")) is not None
    assert detect_captcha(_obs(
        url="https://www.walmart.com/",
        text="Activate and hold the button to confirm that you’re human.",
        controls=30,
    )) is not None


def test_perimeterx_press_and_hold_page() -> None:
    signal = detect_captcha(_obs(
        url="https://www.example-shop.com/product/123",
        title="Access to this page has been denied",
        text="Press & Hold to confirm you are a human (and not a bot). Reference ID 0c1f...",
    ))
    assert signal is not None and signal.source == "title"


def test_perimeterx_modal_over_a_busy_page() -> None:
    """PerimeterX can lay its challenge over a full page: many controls, but
    its own sentence is still unambiguous."""
    signal = detect_captcha(_obs(
        url="https://www.example-shop.com/search?q=pasta",
        title="Pasta | Example Shop",
        text="Results for pasta ... Before we continue... Press & Hold to confirm you are a human (and not a bot).",
        controls=48,
    ))
    assert signal is not None


def test_doordash_cloudflare_page_from_a_real_run() -> None:
    signal = detect_captcha(_obs(
        url="https://www.doordash.com/store/chipotle-waterloo-36154775/81102878/",
        title="Just a moment...",
        text=("www.doordash.com Performing security verification This website uses a security service to "
              "protect against malicious bots. This page is displayed while the website verifies you are "
              "not a bot. Ray ID: a43e008aaca95407 Performance and Security"),
        controls=2,
    ))
    assert signal is not None
    assert signal.source == "title"
    assert "doordash.com" in signal.reason


@pytest.mark.parametrize("text", [
    "www.doordash.com Verify you are human by completing the action below.",
    "www.doordash.com needs to review the security of your connection before proceeding.",
    "Checking if the site connection is secure",
    "Checking your browser before accessing doordash.com.",
    "One more step Please complete the security check to access doordash.com",
])
def test_cloudflare_challenge_wordings(text: str) -> None:
    page = _obs(url="https://www.doordash.com/", title="doordash.com", text=text, controls=2)
    assert detect_captcha(page) is not None


def test_cloudflare_challenge_query_keys() -> None:
    signal = detect_captcha(_obs(url="https://www.doordash.com/?__cf_chl_rt_tk=abc.def-123", title="DoorDash"))
    assert signal is not None and signal.source == "url"


def test_cloudflare_attention_required_title() -> None:
    assert detect_captcha(_obs(title="Attention Required! | Cloudflare", controls=3)) is not None


def test_amazon_captcha_served_at_the_requested_url() -> None:
    """Amazon serves its captcha in place of the page asked for, so only the
    text gives it away."""
    signal = detect_captcha(_obs(
        url="https://www.amazon.ca/s?k=wireless+mouse",
        title="Amazon.ca",
        text=("Enter the characters you see below Sorry, we just need to make sure you're not a robot. "
              "For best results, please make sure your browser is accepting cookies. Type the characters "
              "you see in this image: Try different image Continue shopping"),
        controls=5,
    ))
    assert signal is not None and signal.source == "text"


def test_amazon_validate_captcha_url() -> None:
    signal = detect_captcha(_obs(url="https://www.amazon.com/errors/validateCaptcha?amzn=abc&amzn-r=%2F"))
    assert signal is not None and signal.match == "/errors/validatecaptcha"


def test_google_sorry_url() -> None:
    signal = detect_captcha(_obs(url="https://www.google.com/sorry/index?continue=https://www.google.com/search"))
    assert signal is not None and signal.match == "/sorry/index"


def test_google_unusual_traffic_wording() -> None:
    signal = detect_captcha(_obs(
        url="https://www.google.com/somewhere",
        text="Our systems have detected unusual traffic from your computer network.",
    ))
    assert signal is not None


def test_imperva_pardon_our_interruption() -> None:
    assert detect_captcha(_obs(title="Pardon Our Interruption", controls=2)) is not None


def test_weak_wording_on_a_near_empty_page() -> None:
    assert detect_captcha(_obs(text="Please verify you are a human. Press and hold.", controls=1)) is not None
    assert detect_captcha(_obs(text="Solve this captcha to continue", controls=0)) is not None


# ---- Normal pages are not --------------------------------------------------


def test_normal_page_returns_none() -> None:
    assert detect_captcha(_obs(url="https://en.wikipedia.org/wiki/Toronto", text="Toronto is a city",
                               controls=40)) is None


def test_challenge_butter_is_groceries_not_a_challenge() -> None:
    assert detect_captcha(_obs(
        url="https://www.walmart.ca/en/ip/challenge-butter-salted-454g/6000197259491",
        title="Challenge Butter Salted 454g | Walmart Canada",
        text="Challenge Butter Salted 454g $6.97 Add to cart",
        controls=40,
    )) is None


@pytest.mark.parametrize("url", [
    "https://www.amazon.ca/Drano-Max-Gel-Clog-Remover-blocked/dp/B000",
    "https://www.walmart.ca/en/browse/blocked/123",
    "https://www.example.com/captcha-book/dp/1",
    "https://en.wikipedia.org/wiki/CAPTCHA",
    "https://www.doordash.com/store/sorry-not-sorry-bakery-1/",
])
def test_signal_words_inside_ordinary_urls(url: str) -> None:
    assert detect_captcha(_obs(url=url, title="A product", text="Add to cart", controls=30)) is None


def test_recaptcha_notice_on_a_short_login_form() -> None:
    """Sites that hide the reCAPTCHA badge must print this notice instead, so
    it sits on many short sign-in and checkout forms."""
    assert detect_captcha(_obs(
        url="https://www.doordash.com/consumer/login/",
        title="Sign In | DoorDash",
        text=("Sign in or sign up Email Password Forgot password? Continue This site is protected by "
              "reCAPTCHA and the Google Privacy Policy and Terms of Service apply."),
        controls=4,
    )) is None


def test_press_and_hold_in_a_product_description() -> None:
    assert detect_captcha(_obs(
        url="https://www.amazon.ca/dp/B0WIRELESS",
        title="Logitech Wireless Mouse : Amazon.ca",
        text="To pair, press and hold the connect button for 3 seconds until the light blinks.",
        controls=60,
    )) is None


def test_checkout_and_account_wordings() -> None:
    assert detect_captcha(_obs(title="Checkout", text="One more step: confirm your pickup time.", controls=5)) is None
    assert detect_captcha(_obs(title="Account", text="Verify your identity to change your password.",
                               controls=25)) is None


def test_articles_that_mention_bot_checks() -> None:
    assert detect_captcha(_obs(
        url="https://en.wikipedia.org/wiki/Cloudflare",
        title="Cloudflare - Wikipedia",
        text=("Cloudflare, Inc. is an American company that provides content delivery network services. "
              "Its challenge page shows Just a moment while it checks your browser. Access denied errors..."),
        controls=80,
    )) is None
    assert detect_captcha(_obs(title="just a moment - Google Search", controls=40)) is None


def test_access_denied_is_a_block_not_a_check() -> None:
    """A plain access-denied page has nothing a person could do."""
    assert detect_captcha(_obs(title="Access Denied", text="Access Denied You don't have permission to access "
                               "this resource. Reference #18.abc", controls=0)) is None


def test_unknown_control_count_uses_strong_signals_only() -> None:
    assert detect_page("https://x.test/", "Shop", "press and hold", controls=None) is None
    assert detect_page("https://x.test/", "Just a moment...", "", controls=None) is not None


# ---- Where the run was going ------------------------------------------------


def _b64(path: str) -> str:
    return base64.b64encode(path.encode()).decode().rstrip("=")


def test_walmart_block_page_names_its_destination() -> None:
    url = "https://www.walmart.ca/blocked?url=L2Vu&uuid=c302fdc0&vid=f9559fb4&g=b"
    assert intended_url(url) == "https://www.walmart.ca/en"
    deeper = f"https://www.walmart.ca/blocked?url={_b64('/en/search?q=pasta sauce')}&g=b"
    assert intended_url(deeper) == "https://www.walmart.ca/en/search?q=pasta sauce"


@pytest.mark.parametrize("raw", ["%%%", _b64("//evil.example/x"), _b64("javascript:alert(1)"), ""])
def test_walmart_block_page_with_a_bad_destination(raw: str) -> None:
    assert intended_url(f"https://www.walmart.ca/blocked?url={raw}") is None


def test_google_sorry_continue_stays_on_google() -> None:
    target = "https://www.google.com/search?q=pasta"
    assert intended_url(f"https://www.google.com/sorry/index?continue={target}&q=x") == target
    assert intended_url("https://www.google.com/sorry/index?continue=https://evil.example/") is None


def test_amazon_return_path() -> None:
    assert intended_url("https://www.amazon.ca/errors/validateCaptcha?amzn=abc&amzn-r=%2Fs%3Fk%3Dmouse") == \
        "https://www.amazon.ca/s?k=mouse"


def test_cloudflare_keys_are_dropped_and_the_rest_kept() -> None:
    assert intended_url("https://www.doordash.com/store/1/?pickup=false&__cf_chl_rt_tk=abc") == \
        "https://www.doordash.com/store/1/?pickup=false"


def test_ordinary_urls_name_no_destination() -> None:
    assert intended_url("https://www.walmart.ca/en/search?q=pasta") is None
    assert intended_url("about:blank") is None


# ---- The cheap look a waiting run takes --------------------------------------


class _Page:
    url = "https://www.walmart.ca/blocked?url=L2Vu"

    def __init__(self, document: str) -> None:
        self.document = document
        self.evaluated: list[str] = []

    async def title(self) -> str:
        return "Verify Your Identity"

    async def content(self) -> str:
        return self.document

    async def evaluate(self, script: str, *args: object) -> object:  # pragma: no cover — must not run
        self.evaluated.append(script)
        raise AssertionError("probe ran a script in the page's world")


async def test_probe_reads_url_title_and_a_document_digest() -> None:
    page = _Page("<html><body>We like real shoppers, not robots!</body></html>")
    first = await probe(page)
    assert first.url == page.url and first.title == "Verify Your Identity"
    assert (await probe(page)).digest == first.digest
    page.document = "<html><body>Thanks!</body></html>"
    assert (await probe(page)).digest != first.digest
    assert page.evaluated == []
