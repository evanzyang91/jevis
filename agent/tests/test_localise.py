"""Canadian storefront mapping."""

from __future__ import annotations

from agent.planner import localise


def test_walmart_us_rewrites_to_ca() -> None:
    assert localise("https://www.walmart.com/") == "https://www.walmart.ca/"


def test_bare_amazon_rewrites_to_ca() -> None:
    assert localise("https://amazon.com/dp/foo") == "https://www.amazon.ca/dp/foo"


def test_indeed_uses_ca_subdomain() -> None:
    assert localise("https://www.indeed.com/") == "https://ca.indeed.com/"


def test_unlisted_host_passes_through() -> None:
    assert localise("https://en.wikipedia.org/wiki/Toronto") == "https://en.wikipedia.org/wiki/Toronto"


def test_already_canadian_domain_is_untouched() -> None:
    assert localise("https://www.walmart.ca/") == "https://www.walmart.ca/"
