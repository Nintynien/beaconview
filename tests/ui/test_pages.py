"""Every page in the browser: renders its data, no console errors, both themes, no sideways scroll on a phone."""

import re

import pytest
from playwright.sync_api import expect

PAGES = [
    ("/", "#seriesChart svg path"),
    ("/explore", "#tsChart svg path"),
    ("/settings", "#logsTable tbody tr"),
]


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("path,ready", PAGES)
def test_page_renders_without_errors(site, page, errors, path, ready, scheme):
    page.emulate_media(color_scheme=scheme)
    page.goto(site.base + path)
    expect(page.locator(ready).first).to_be_visible()
    page.wait_for_timeout(500)                                # late fetches (status, live) settle
    assert errors == []


@pytest.mark.parametrize("path,ready", PAGES)
def test_phone_width_has_no_sideways_scroll(site, page, errors, path, ready):
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(site.base + path)
    expect(page.locator(ready).first).to_be_visible()
    width = page.evaluate("[document.documentElement.clientWidth, document.documentElement.scrollWidth]")
    assert width[1] <= width[0], f"page is {width[1]}px wide in a {width[0]}px screen"
    assert errors == []


def test_overview_fills_in_and_links_into_explore(site, page, errors):
    page.goto(site.base + "/?range=6h")
    expect(page.locator(".tile")).to_have_count(7)
    expect(page.locator("#servicesTable tbody tr").first).to_be_visible()
    for table in ("#statusTable", "#pathsTable", "#ipsTable", "#uasTable", "#hostsTable"):
        expect(page.locator(table + " tbody tr").first).to_be_visible()
    router = page.locator("#servicesTable tbody td.name a").first
    name = router.inner_text()
    router.click()
    expect(page).to_have_url(re.compile(r"/explore\?.*router="))
    expect(page.locator(".chip")).to_contain_text(name)
    assert errors == []


def test_settings_lock_save_and_confirm(site, page, errors):
    page.goto(site.base + "/settings")
    rollup = page.locator("#f_rollup_retention_days")
    expect(rollup).to_be_disabled()
    expect(page.locator(".field.locked .lock")).to_have_text("set by ROLLUP_RETENTION_DAYS")
    # a harmless change saves and applies
    page.fill("#f_client_ip_header", "X-Real-IP")
    page.click("#saveBtn")
    expect(page.locator("#saveMsg")).to_contain_text("Saved")
    page.fill("#f_client_ip_header", "")
    page.click("#saveBtn")
    expect(page.locator("#saveMsg")).to_contain_text("Saved")
    # keeping requests for less time asks first, with what it would delete
    page.fill("#f_retention_days", "0.01")
    page.click("#saveBtn")
    expect(page.locator("#confirmBox")).to_be_visible()
    expect(page.locator("#confirmText")).to_contain_text("This deletes")
    page.click("#confirmNo")
    expect(page.locator("#confirmBox")).to_be_hidden()
    assert site.app.settings.retention_days == 7                 # nothing was saved
    assert errors == []


def test_highlights_learn_first(site, page, errors):
    page.goto(site.base + "/")                               # two hours of history: nothing to compare with yet
    expect(page.locator("#highlightsList li")).to_contain_text("Learning what normal looks like")
    assert errors == []


def test_highlights_link_into_explore(site, page, errors):
    def fake(route):
        route.fulfill(json={"from": 0, "to": 1, "days": 7, "state": "ready", "items": [
            {"kind": "errors", "severity": "critical", "title": "api@docker: 40% server errors",
             "detail": "40 5xx of 100 requests; usually 0.1% at this time",
             "filters": [["router", "api@docker"], ["status", "5xx"]]},
            {"kind": "client", "severity": "warning", "title": "New client 203.0.113.9",
             "detail": "300 requests (12% of traffic)", "filters": [["ip", "203.0.113.9"]]}]})
    page.route("**/api/highlights*", fake)
    page.goto(site.base + "/")
    items = page.locator("#highlightsList li")
    expect(items).to_have_count(2)
    expect(items.first.locator(".sev")).to_have_text("▲Critical")      # icon and word, never color alone
    expect(page.locator("#highlightsSub")).to_have_text("This range vs the same time on the past 7 days")
    items.first.locator("a").click()
    expect(page).to_have_url(re.compile(r"/explore\?.*router=api%40docker.*status=5xx"))
    expect(page.locator(".chip")).to_have_count(2)
    assert errors == []


def test_countries_with_credit(site, page, errors):
    page.goto(site.base + "/?range=6h")
    expect(page.locator("#countriesCard")).to_be_visible()
    expect(page.locator("#countriesTable tbody td.name a").first).to_have_text(
        re.compile(r"United States|Germany|France|Japan|Brazil|Unknown"))
    expect(page.locator("#credit a")).to_have_attribute("href", "https://db-ip.com")
    assert errors == []


def test_country_map_shades_explains_and_links(site, page, errors):
    page.goto(site.base + "/?range=6h")
    us = page.locator('#countriesMap path[data-c="US"]')
    expect(us).to_have_class(re.compile(r"\bq[1-5]\b"))
    expect(page.locator('#countriesMap path[data-c="CA"]')).to_have_class("q0")      # no requests: neutral
    expect(page.locator("#countriesMap .maplegend span").first).to_have_text("none")
    expect(page.locator("#countriesMap .hint")).to_contain_text("Unknown location")     # ZZ is not on the map
    us.hover()
    expect(page.locator("#countriesMap .tip")).to_contain_text("United States")
    us.click()
    expect(page).to_have_url(re.compile(r"/explore\?.*country=US"))
    assert errors == []
