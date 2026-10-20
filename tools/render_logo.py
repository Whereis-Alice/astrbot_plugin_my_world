"""Render both PNG logos from the canonical SVG (requires Playwright + Chromium).

    python tools/render_logo.py
"""
from pathlib import Path


def main() -> None:
    from playwright.sync_api import sync_playwright

    root = Path(__file__).resolve().parents[1]
    source = (root / "logo.svg").read_text(encoding="utf-8")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 512, "height": 512}, device_scale_factor=1)
        page.set_content('<body style="margin:0;background:transparent">' + source + '</body>')
        png = page.screenshot(omit_background=True)
        browser.close()
    for name in ("logo.png", "pages/world_editor/assets/logo.png"):
        (root / name).write_bytes(png)
        print(f"written: {name}")
    (root / "pages/world_editor/assets/logo.svg").write_text(source, encoding="utf-8")


if __name__ == "__main__":
    main()
