"""Explore in the browser: filters, the chart and log linked both ways, breakdown, live updates and Pause."""

import re

from playwright.sync_api import expect


def chart_box(page):
    return page.locator("#tsChart").bounding_box()


def test_filter_updates_chart_log_and_url(site, page, errors):
    page.goto(site.base + "/explore?range=6h")
    expect(page.locator("#logsBody tr").first).to_be_visible()
    page.click("#addBtn")
    page.select_option("#addField", "status")
    page.fill("#addValue", "5xx")
    page.click("#addForm button[type=submit]")
    expect(page.locator(".chip")).to_contain_text("5xx")
    expect(page).to_have_url(re.compile(r"status=5xx"))
    # the filtered page arrives asynchronously: wait until every row shown is a 5xx
    page.wait_for_function("""() => { const s = [...document.querySelectorAll('#logsBody tr td:nth-child(2)')];
      return s.length > 0 && s.every(td => td.innerText.trim().startsWith('5')); }""")
    expect(page.locator("#logsSub")).to_contain_text("matching")
    # removing the chip brings everything back
    page.locator(".chip button").click()
    expect(page.locator(".chip")).to_have_count(0)
    assert errors == []


def test_hovering_a_request_marks_its_bar(site, page, errors):
    page.goto(site.base + "/explore?range=6h")
    expect(page.locator("#logsBody tr").first).to_be_visible()
    page.locator("#logsBody tr").nth(2).hover()
    expect(page.locator("#tsChart .hover-band.marked")).to_have_attribute("visibility", "visible")
    page.mouse.move(5, 5)
    expect(page.locator("#tsChart .hover-band")).to_have_attribute("visibility", "hidden")
    assert errors == []


def test_clicking_a_bar_jumps_the_log_and_newer_walks_back_up(site, page, errors):
    page.goto(site.base + "/explore?range=6h&status=5xx")
    expect(page.locator("#logsBody tr").first).to_be_visible()
    box = chart_box(page)
    page.mouse.click(box["x"] + box["width"] * 0.6, box["y"] + 150)
    expect(page.locator("#logsSub")).to_contain_text("back")
    expect(page.locator("#newerBtn")).to_be_visible()
    expect(page.locator("#newestBtn")).to_be_visible()
    for _ in range(20):                                       # page up until caught up
        if page.locator("#newerBtn").is_hidden():
            break
        page.click("#newerBtn")
        page.wait_for_timeout(300)
    expect(page.locator("#newerBtn")).to_be_hidden()
    expect(page.locator("#logsSub")).to_contain_text("newest first")
    assert errors == []


def test_breakdown_sorts_and_filters(site, page, errors):
    page.goto(site.base + "/explore?range=6h&view=breakdown&group=host")
    expect(page.locator("#bdTable tbody tr").first).to_be_visible()
    page.locator("#bdTable th", has_text="5xx").click()
    expect(page.locator("#bdTable th[aria-sort=descending]")).to_contain_text("5xx")
    expect(page).to_have_url(re.compile(r"sort=status_5xx"))
    host = page.locator("#bdTable tbody tr").first.get_attribute("data-value")
    page.locator("#bdTable tbody tr").first.locator("td.acts button", has_text="=").click()
    expect(page.locator(".chip")).to_contain_text(host)
    # grouped by nothing: one totals row
    page.select_option("#groupSel", "")
    expect(page.locator("#bdTable tbody tr")).to_have_count(1)
    expect(page.locator("#bdTable tbody tr")).to_contain_text("All matching requests")
    assert errors == []


def test_live_pushes_new_requests_and_redraws(site, page, errors):
    series = []
    page.on("request", lambda r: series.append(r.url) if "/api/series" in r.url else None)
    page.goto(site.base + "/explore?range=5m")
    expect(page.locator("#logsBody tr").first).to_be_visible()
    expect(page.locator("#liveBtn")).to_have_text("Pause")
    before = len(series)
    site.append(15)
    expect(page.locator("#logsBody tr.fresh").first).to_be_visible(timeout=10_000)
    page.wait_for_timeout(3000)
    assert len(series) > before, "the chart did not redraw on new data"
    expect(page.locator("#logsSub")).to_contain_text("live")
    assert errors == []


def test_pause_fixes_the_range_and_stops_updates(site, page, errors):
    page.goto(site.base + "/explore?range=5m")
    expect(page.locator("#logsBody tr").first).to_be_visible()
    page.click("#liveBtn")
    expect(page).to_have_url(re.compile(r"from=\d+&to=\d+"))
    expect(page.locator("#liveBtn")).to_have_text("Live")
    fresh = page.locator("#logsBody tr.fresh").count()
    site.append(10)
    page.wait_for_timeout(3000)
    assert page.locator("#logsBody tr.fresh").count() == fresh
    page.click("#liveBtn")
    expect(page).to_have_url(re.compile(r"range=5m"))
    expect(page.locator("#liveBtn")).to_have_text("Pause")
    assert errors == []


def test_reading_the_log_keeps_its_place(site, page, errors):
    page.goto(site.base + "/explore?range=1h")
    expect(page.locator("#logsBody tr").nth(60)).to_be_attached()
    page.evaluate("document.getElementById('logWrap').scrollTop = 600")
    anchor = page.evaluate("""() => { const w = document.getElementById('logWrap').getBoundingClientRect();
      const tr = [...document.querySelectorAll('#logsBody tr')].find(t => t.getBoundingClientRect().top > w.top + 40);
      tr.id = 'reading'; return tr.getBoundingClientRect().top; }""")
    site.append(12)
    expect(page.locator("#unseenBtn")).to_be_visible(timeout=10_000)
    expect(page.locator("#unseenBtn")).to_contain_text("new ↑")
    moved = page.evaluate("document.getElementById('reading').getBoundingClientRect().top") - anchor
    assert abs(moved) <= 2, f"the row being read moved {moved}px"
    page.click("#unseenBtn")
    expect(page.locator("#unseenBtn")).to_be_hidden()
    assert errors == []


def test_csv_buttons_carry_the_query(site, page, errors):
    page.goto(site.base + "/explore?range=6h&status=2xx")
    expect(page.locator("#tsChart svg path").first).to_be_visible()
    with page.expect_download() as dl:
        page.click("#logsCsv")
    d = dl.value
    assert "kind=logs" in d.url and "status=2xx" in d.url and d.suggested_filename.startswith("beaconview-logs-")
    head = open(d.path(), encoding="utf-8").readline()
    assert head.startswith("time_utc,instance,method") and "country" in head
    with page.expect_download() as dl:
        page.click("#vizCsv")
    assert "kind=series" in dl.value.url
    assert errors == []
