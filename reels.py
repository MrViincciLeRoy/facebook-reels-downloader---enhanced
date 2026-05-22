import asyncio
from playwright.async_api import async_playwright
import os
import subprocess
import csv
import sys
import json
import re


def parse_cookies_file(path):
    raw = open(path).read().strip()
    if not raw:
        return None, "empty file"

    # Try JSON first
    try:
        cookies = json.loads(raw)
        clean = []
        for c in cookies:
            clean.append({
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", ".facebook.com"),
                "path": c.get("path", "/"),
                "secure": c.get("secure", False),
                "httpOnly": c.get("httpOnly", False),
            })
        return clean, None
    except json.JSONDecodeError:
        pass

    # Try Netscape txt format
    try:
        clean = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            clean.append({
                "domain": parts[0],
                "path": parts[2],
                "secure": parts[3].upper() == "TRUE",
                "name": parts[5],
                "value": parts[6],
                "httpOnly": False,
            })
        if clean:
            return clean, None
        return None, "no valid cookie lines found"
    except Exception as e:
        return None, str(e)


async def scrape_reels(channel, url, max_count=None):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context()

        cookie_file = None
        for f in ["fb_cookie.json", "fb_cookie.txt", "cookies.json", "cookies.txt"]:
            if os.path.exists(f):
                cookie_file = f
                break

        if cookie_file:
            cookies, err = parse_cookies_file(cookie_file)
            if err:
                print(f"⚠️ Cookie load failed ({cookie_file}): {err} — proceeding without")
            else:
                await context.add_cookies(cookies)
                print(f"✅ Loaded {len(cookies)} cookies from {cookie_file}")
        else:
            print("⚠️ No cookie file found — proceeding without")

        page = await context.new_page()
        await page.goto("https://facebook.com")
        await page.wait_for_timeout(3000)
        await page.goto(url)
        await page.wait_for_timeout(5000)

        try:
            close_btn = page.locator("div[aria-label='Close']")
            if await close_btn.count() > 0:
                await close_btn.first.click()
        except:
            pass

        reel_urls = []
        prev_height = 0

        for _ in range(100):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(4000)

            links = await page.eval_on_selector_all(
                'a',
                "els => els.map(e => e.href).filter(h => h && h.includes('/reel/'))"
            )

            for link in links:
                clean = link.split('/?s=')[0]
                if clean not in reel_urls:
                    reel_urls.append(clean)

            if max_count and len(reel_urls) >= max_count:
                print(f"Reached {max_count} links, stopping.")
                reel_urls = reel_urls[:max_count]
                break

            curr_height = await page.evaluate("document.body.scrollHeight")
            if curr_height == prev_height:
                print("Reached bottom of page.")
                break
            prev_height = curr_height

        await browser.close()
        return reel_urls


def parse_number(text):
    match = re.search(r'([\d.]+)([KM]?)', text)
    if not match:
        return 0
    num, suffix = match.groups()
    num = float(num)
    return int(num * 1_000_000 if suffix == 'M' else num * 1_000 if suffix == 'K' else num)


def rename_by_engagement(output_dir):
    for filename in os.listdir(output_dir):
        if not filename.endswith(".mp4"):
            continue
        views_match = re.search(r'([\d.]+[KM]?) views', filename)
        likes_match = re.search(r'([\d.]+[KM]?) reactions', filename)
        if not views_match or not likes_match:
            continue
        views = parse_number(views_match.group(1))
        likes = parse_number(likes_match.group(1))
        if views == 0:
            continue
        engagement = (likes / views) * 100
        new_name = f"[{engagement:.2f}%] {filename}"
        os.rename(os.path.join(output_dir, filename), os.path.join(output_dir, new_name))
        print(f"Renamed: {new_name}")


def download_reels(channel, reel_urls):
    if not reel_urls:
        print("❌ No reels found. Check cookies or URL.")
        sys.exit(1)

    output_dir = os.path.join("output", channel)
    os.makedirs(output_dir, exist_ok=True)
    csv_path = os.path.join("output", f"{channel}.csv")

    with open(csv_path, "w") as f:
        writer = csv.writer(f)
        for url in reel_urls:
            writer.writerow([url])

    total = len(reel_urls)
    downloaded = 0

    for i, url in enumerate(reel_urls, 1):
        args = [
            "yt-dlp", url,
            "--output", os.path.join(output_dir, "%(title).100s [%(id)s].%(ext)s"),
            "--no-playlist", "--ignore-errors",
            "--retries", "10",
            "--fragment-retries", "10",
            "--sleep-interval", "2",
            "--max-sleep-interval", "5"
        ]

        cookie_file = None
        for f in ["cookies.txt", "fb_cookie.txt", "fb_cookie.json"]:
            if os.path.exists(f):
                cookie_file = f
                break
        if cookie_file:
            args += ["--cookies", cookie_file]

        print(f"({i}/{total}) Downloading: {url}")
        result = subprocess.run(args, capture_output=True, text=True)

        if result.returncode == 0:
            downloaded += 1
            print(f"   ✅ {downloaded}/{total}")
            continue

        print("   ⚠️ Retrying with fallback...")
        fallback = [
            "yt-dlp", url,
            "--format", "bestvideo+bestaudio/best",
            "--output", os.path.join(output_dir, "%(title).100s [%(id)s].%(ext)s"),
            "--no-playlist", "--ignore-errors", "--verbose"
        ]
        if cookie_file:
            fallback += ["--cookies", cookie_file]

        fb_result = subprocess.run(fallback, capture_output=True, text=True)
        if fb_result.returncode == 0:
            downloaded += 1
            print(f"   ✅ fallback ok — {downloaded}/{total}")
        else:
            print("   ❌ failed, logged.")
            with open("failed.txt", "a") as log:
                log.write(url + "\n")

    print(f"\n📋 Summary: ✅ {downloaded} | ❌ {total - downloaded} | 🎯 {downloaded/total*100:.1f}%")
    rename_by_engagement(output_dir)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python reels.py <channel_name> <channel_url> [max_count]")
        sys.exit(1)

    channel = sys.argv[1]
    url = sys.argv[2]
    max_count = int(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3].isdigit() else None

    reel_urls = asyncio.run(scrape_reels(channel, url, max_count))
    print(f"\nFound {len(reel_urls)} reels. Starting downloads...")
    download_reels(channel, reel_urls)
