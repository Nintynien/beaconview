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
