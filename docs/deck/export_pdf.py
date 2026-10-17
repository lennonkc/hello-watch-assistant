# Usage: python docs/deck/export_pdf.py [--png DIR]   (needs `pip install playwright && playwright install chromium`)
"""Export deck.html to hello-watch-assistant-deck.pdf (one 1920×1080 page per slide), next to this script.

--png DIR also writes one screenshot per slide, for visual review. The deck loads Google Fonts, so run it online.
"""
import argparse
import pathlib

from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).resolve().parent
DECK = HERE / "deck.html"
PDF = HERE / "hello-watch-assistant-deck.pdf"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--png", type=pathlib.Path, help="directory for per-slide screenshots")
    args = ap.parse_args()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(DECK.as_uri(), wait_until="networkidle")
        page.evaluate("document.fonts.ready")
        if args.png:
            args.png.mkdir(parents=True, exist_ok=True)
            for i, slide in enumerate(page.query_selector_all(".slide"), 1):
                slide.screenshot(path=str(args.png / f"slide{i:02d}.png"))
        page.emulate_media(media="print")
        page.pdf(path=str(PDF), width="1920px", height="1080px", print_background=True,
                 margin={"top": "0", "right": "0", "bottom": "0", "left": "0"}, prefer_css_page_size=True)
        browser.close()

    if errors:
        raise SystemExit(f"page errors: {errors}")
    print(f"{PDF.relative_to(HERE.parent.parent)}: {PDF.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
