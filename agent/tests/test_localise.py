"""Regional storefront mapping (AGENT_REGION)."""

from __future__ import annotations

from agent.planner import localise


def test_walmart_us_rewrites_to_ca() -> None:
    assert localise("https://www.walmart.com/", region="CA") == "https://www.walmart.ca/"


def test_bare_amazon_rewrites_to_ca() -> None:
    assert localise("https://amazon.com/dp/foo", region="ca") == "https://www.amazon.ca/dp/foo"


def test_indeed_uses_ca_subdomain() -> None:
    assert localise("https://www.indeed.com/", region="CA") == "https://ca.indeed.com/"


def test_unlisted_host_passes_through() -> None:
    assert localise("https://en.wikipedia.org/wiki/Toronto", region="CA") == "https://en.wikipedia.org/wiki/Toronto"


def test_already_canadian_domain_is_untouched() -> None:
    assert localise("https://www.walmart.ca/", region="CA") == "https://www.walmart.ca/"


def test_no_region_leaves_every_url_unchanged() -> None:
    assert localise("https://www.walmart.com/", region="") == "https://www.walmart.com/"


def test_region_comes_from_the_environment(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("AGENT_REGION", "CA")
    assert localise("https://www.walmart.com/") == "https://www.walmart.ca/"
    monkeypatch.delenv("AGENT_REGION")
    assert localise("https://www.walmart.com/") == "https://www.walmart.com/"
