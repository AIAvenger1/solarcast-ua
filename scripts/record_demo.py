#!/usr/bin/env python3
"""Record the demo video and the screenshots used in the README and the slides.

    uv run uvicorn app.main:app --port 8765 &        # the app must be running
    uv run python scripts/record_demo.py             # -> docs/demo.mp4, docs/*.png

Uses the installed Google Chrome (no browser download) and ffmpeg for the mp4.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765/"
DOCS = Path(__file__).resolve().parent.parent / "docs"
RAW = DOCS / "video-raw"
SIZE = {"width": 1280, "height": 800}


def pause(page, ms=1200):
    page.wait_for_timeout(ms)


def scroll_to(page, selector):
    page.locator(selector).scroll_into_view_if_needed()
    page.evaluate("s => document.querySelector(s).scrollIntoView({behavior: 'smooth', block: 'center'})", selector)
    pause(page, 1400)


def main():
    DOCS.mkdir(exist_ok=True)
    shutil.rmtree(RAW, ignore_errors=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome")

        # ---- the walkthrough, recorded ----
        ctx = browser.new_context(viewport=SIZE, record_video_dir=str(RAW), record_video_size=SIZE, locale="uk-UA")
        page = ctx.new_page()
        code = os.environ.get("SOLARCAST_ACCESS_TOKEN", "")
        page.goto(URL + (f"#key={code}" if code else ""))      # unlocks the Claude-written plan
        page.wait_for_selector("#bars svg")
        pause(page, 2500)                                   # Kyiv forecast, loaded on start
        page.screenshot(path=str(DOCS / "main.png"), full_page=True)

        hit = page.locator("#hours .hit")                   # read the hourly curve
        scroll_to(page, "#hours")
        box = hit.bounding_box()
        for f in (0.35, 0.42, 0.5, 0.56, 0.62):
            page.mouse.move(box["x"] + box["width"] * f, box["y"] + box["height"] * 0.4, steps=12)
            pause(page, 500)
        page.screenshot(path=str(DOCS / "hourly.png"))

        scroll_to(page, "#bars")                            # pick another day
        page.locator("#bars .day").nth(1).click()
        pause(page, 1800)
        scroll_to(page, "#hours")
        pause(page, 1200)

        page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")   # another city, bigger station
        pause(page, 1200)
        page.fill("#city", "")
        page.type("#city", "Одеса", delay=140)
        page.wait_for_selector("#options button")
        pause(page, 900)
        page.locator("#options button").first.click()
        pause(page, 600)
        page.fill("#kwp", "5")
        page.select_option("#azimuth", "45")
        page.fill("#battery", "10")
        pause(page, 900)
        page.click("#go")
        page.wait_for_function("document.querySelector('.sub')?.textContent.includes('Одеса')")
        pause(page, 2500)
        scroll_to(page, "#table")
        pause(page, 1500)

        scroll_to(page, "#adviceBtn")                       # the weekly plan
        page.click("#adviceBtn")
        page.wait_for_selector("#advice .source", timeout=180_000)
        pause(page, 1500)
        page.evaluate("document.querySelector('#advice').scrollIntoView({behavior: 'smooth', block: 'start'})")
        pause(page, 3500)
        page.locator("#advice").screenshot(path=str(DOCS / "advice.png"))
        source = page.inner_text("#advice .source")
        page.evaluate("document.querySelector('#advice .source').scrollIntoView({behavior: 'smooth', block: 'end'})")
        pause(page, 3000)
        video = Path(page.video.path())
        ctx.close()

        # ---- a phone-sized screenshot ----
        phone = browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2, locale="uk-UA")
        phone.goto(URL)
        phone.wait_for_selector("#bars svg")
        pause(phone, 800)
        phone.screenshot(path=str(DOCS / "mobile.png"), full_page=True)
        browser.close()

    out = DOCS / "demo.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-crf", "24", "-movflags", "+faststart", str(out)], check=True)
    shutil.rmtree(RAW, ignore_errors=True)
    print("advice source:", source)
    print("saved", out)


if __name__ == "__main__":
    main()
