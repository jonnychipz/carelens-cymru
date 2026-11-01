"""Discover brand-image links only on websites already listed by CIW.

Images remain hosted by their original websites; the release does not claim
ownership, download/repackage brand assets, or infer a home-specific logo from
a shared provider mark. Unavailable and unverified records remain explicit.
"""
import argparse
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent


def website_url(value):
    value = (value or "").strip()
    if re.fullmatch(r"(?:www\.)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s]*)?", value, re.I):
        value = "https://" + value
    parsed = urlparse(value)
    return value if parsed.scheme in ["http", "https"] and parsed.hostname and not parsed.username else None


def candidate_url(value, base):
    url = urljoin(base, value or "")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return None
    if re.search(r"(?:token|signature|credential|api[_-]?key)=", parsed.query, re.I):
        return None
    return url


def fetch_record(session, url):
    record = {"listed_website": url, "verified": False, "logo_status": "not_verified"}
    try:
        response = session.get(url, timeout=(3, 6), stream=True)
        response.raise_for_status()
        if "text/html" not in response.headers.get("Content-Type", ""):
            record["logo_status"] = "website_not_html"
            response.close()
            return record
        payload = bytearray()
        for chunk in response.iter_content(16384):
            payload.extend(chunk)
            if len(payload) > 1_500_000:
                break
        response.close()
        final = response.url
        soup = BeautifulSoup(bytes(payload), "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        record.update(website_url=final, website_title=title, source_url=final,
                      checked_at=datetime.now(timezone.utc).isoformat(), website_reachable=True)
        if re.search(r"domain.{0,25}(?:for sale|parked)|buy this domain|website suspended", title, re.I):
            record["logo_status"] = "website_identity_needs_review"
            return record
        candidates = []
        for image in soup.find_all("img"):
            description = " ".join(str(image.get(key, "")) for key in ["alt", "class", "id", "src"])
            if not re.search(r"\blogo\b|[_/-]logo[._/-]|brand[_-]mark", description, re.I):
                continue
            src = image.get("src") or image.get("data-src") or image.get("data-lazy-src")
            if not src or src.startswith("data:"):
                src = image.get("data-src") or image.get("data-lazy-src")
            candidate = candidate_url(src, final)
            if candidate:
                candidates.append((candidate, "Website logo", image.get("alt", "")))
        for image in soup.find_all("link"):
            rel = " ".join(image.get("rel") or [])
            if re.search(r"(?:^|\s)(?:icon|apple-touch-icon)(?:\s|$)", rel, re.I):
                candidate = candidate_url(image.get("href"), final)
                if candidate:
                    candidates.append((candidate, "Website icon", ""))
        seen = set()
        for candidate, kind, alt in candidates[:8]:
            if candidate in seen:
                continue
            seen.add(candidate)
            image_response = session.get(candidate, timeout=(3, 5), stream=True)
            try:
                if image_response.status_code != 200 or not image_response.headers.get("Content-Type", "").startswith("image/"):
                    continue
                if int(image_response.headers.get("Content-Length") or 0) > 5_000_000:
                    continue
                prefix = next(image_response.iter_content(1024), b"")
                if not prefix:
                    continue
                resolved = candidate_url(image_response.url, final)
                if not resolved:
                    continue
                record.update(verified=True, logo_status="source_image_verified", logo_url=resolved,
                              image_kind=kind, image_alt=alt,
                              attribution="Original website image; may identify the provider rather than this individual home. Rights remain with its owner.")
                return record
            finally:
                image_response.close()
        record["logo_status"] = "no_verified_logo_or_icon"
        return record
    except (requests.RequestException, ValueError) as error:
        record.update(logo_status="source_unavailable",
                      error=f"{type(error).__name__}: {str(error)[:180]}")
        return record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    data = json.loads((ROOT / "catalogue.json").read_text(encoding="utf-8"))
    existing_path = ROOT / "customer-branding.json"
    existing = json.loads(existing_path.read_text(encoding="utf-8")) if existing_path.exists() else {"websites": {}}
    websites = existing["websites"]
    urls = Counter(website_url(s.get("website")) for s in data["services"])
    urls.pop(None, None)
    session = requests.Session()
    session.headers["User-Agent"] = "CareLens-Cymru-public-website-reference/1.0"
    ordered = sorted(urls, key=lambda url: (-urls[url], url))
    if args.limit:
        ordered = ordered[:args.limit]
    for index, url in enumerate(ordered, 1):
        if url not in websites:
            websites[url] = fetch_record(session, url)
        if index % 10 == 0 or index == len(ordered):
            records = {s["urn"]: websites.get(website_url(s.get("website")), {
                "verified": False, "logo_status": "website_not_published" if not website_url(s.get("website"))
                else "not_checked", "listed_website": website_url(s.get("website"))}) for s in data["services"]}
            output = {"meta": {"source": "Websites supplied in the CIW public directory",
                               "checked_at": datetime.now(timezone.utc).isoformat(),
                               "image_policy": "Source-hosted logo/icon links, not copied assets. Provider marks are not asserted to be individual care-home logos.",
                               "verified_image_homes": sum(bool(r.get("verified")) for r in records.values()),
                               "checked_websites": len(websites),
                               "listed_websites": len(urls)},
                      "websites": websites, "services": records}
            temporary = existing_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(existing_path)
            print(f"Brand references: {index}/{len(ordered)} websites; {output['meta']['verified_image_homes']} homes with source-verified images", flush=True)
        time.sleep(.08)
    session.close()


if __name__ == "__main__":
    main()
