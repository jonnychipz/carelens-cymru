"""Validate the complete archived corpus and record explicit extraction-review needs."""
import hashlib
import json
import re
import sqlite3
from collections import Counter

import fitz

from collect import ROOT, THEMES, write_json


def main():
    data = json.loads((ROOT / "catalogue.json").read_text(encoding="utf-8"))
    services = data["services"]
    raw = json.loads((ROOT / "source" / "directory.json").read_text(encoding="utf-8"))
    errors = []
    review = []
    urns = {s["urn"] for s in services}
    raw_urns = {s["urn"] for s in raw["services"]}
    if len(services) != raw["total"] or urns != raw_urns or len(urns) != len(services):
        errors.append("Catalogue does not exactly match the unique filtered directory inventory.")
    with sqlite3.connect(ROOT / "catalogue.sqlite") as db:
        integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            errors.append(f"SQLite integrity: {integrity}")
        if db.execute("PRAGMA foreign_key_check").fetchall():
            errors.append("Database contains foreign-key inconsistencies.")
        db_urns = {row[0] for row in db.execute("SELECT urn FROM services")}
        if db_urns != urns:
            errors.append("Database and JSON service sets differ.")
        for service in services:
            report = service["report"]
            urn = service["urn"]
            stored = db.execute("SELECT status,sha256,pages,summary FROM reports WHERE service_urn=?", (urn,)).fetchone()
            if not stored or stored[0] != report["status"]:
                errors.append(f"{urn}: database report status missing or inconsistent.")
            if report["status"] == "pending":
                errors.append(f"{urn}: still pending.")
            if not service["inspection"]:
                if report["status"] != "not_published":
                    errors.append(f"{urn}: no inspection metadata but unexpected archive status.")
                continue
            if report["status"] != "downloaded":
                review.append({"urn": urn, "name": service["name"], "reason": report["status"],
                               "error": report.get("error")})
                continue
            path = ROOT / report["file"]
            if not path.is_file():
                errors.append(f"{urn}: archived PDF missing.")
                continue
            binary = path.read_bytes()
            if not binary.startswith(b"%PDF") or len(binary) != report["bytes"]:
                errors.append(f"{urn}: file signature or size mismatch.")
            if hashlib.sha256(binary).hexdigest() != report["sha256"]:
                errors.append(f"{urn}: SHA-256 mismatch.")
            disposition = report.get("content_disposition")
            expected_id = re.sub(r"[^a-z0-9]", "", service["inspection"]["urn"].lower())
            if disposition and expected_id not in re.sub(r"[^a-z0-9]", "", disposition.lower()):
                errors.append(f"{urn}: downloaded report identifier differs from latest inspection metadata.")
            with fitz.open(path) as pdf:
                page_count = len(pdf)
                if page_count != report["pages"]:
                    errors.append(f"{urn}: PDF page count mismatch.")
            rows = db.execute("SELECT page FROM report_pages WHERE service_urn=? ORDER BY page", (urn,)).fetchall()
            if [r[0] for r in rows] != list(range(1, page_count + 1)):
                errors.append(f"{urn}: database full-text pages are incomplete.")
            if stored and (stored[1] != report["sha256"] or stored[2] != page_count or stored[3] != report["summary"]):
                errors.append(f"{urn}: database extraction differs from catalogue JSON.")
            if not (ROOT / "text" / f"{urn}.txt").is_file():
                errors.append(f"{urn}: page-labelled text file missing.")
            if not report.get("summary"):
                review.append({"urn": urn, "name": service["name"], "reason": "Official summary heading not recognised"})
            elif report.get("summary_kind") != "Official CIW summary":
                review.append({"urn": urn, "name": service["name"],
                               "reason": "Source Summary section blank/absent; labelled theme-based synopsis supplied"})
            if report.get("summary") and len(report["summary"]) > 6500:
                review.append({"urn": urn, "name": service["name"], "reason": "Unusually long summary; inspect section boundaries",
                               "characters": len(report["summary"])})
            if report.get("table_review_pages"):
                review.append({"urn": urn, "name": service["name"], "reason": "Action table layout requires review",
                               "pages": report["table_review_pages"]})
            for finding in report.get("findings", []):
                if not 1 <= finding["page"] <= page_count:
                    errors.append(f"{urn}: evidence page out of range.")
            for section in report.get("sections", []):
                if any(not 1 <= p <= page_count for p in section["pages"]):
                    errors.append(f"{urn}: section page out of range.")
    statuses = dict(Counter(s["report"]["status"] for s in services))
    rating_counts = {theme: dict(Counter(s["ratings"].get(theme, "Not published") for s in services))
                     for theme in THEMES}
    result = {
        "inventory_services": len(services), "expected_services": raw["total"],
        "ratings_published_services": sum(bool(s["ratings"]) for s in services),
        "statuses": statuses, "rating_counts": rating_counts,
        "report_synopses": sum(bool(s["report"].get("summary")) for s in services),
        "official_summaries": sum(s["report"].get("summary_kind") == "Official CIW summary"
                                  for s in services if s["report"]["status"] == "downloaded"),
        "archived_pdf_files": len(list((ROOT / "reports").glob("*.pdf"))),
        "archived_pdf_bytes": sum(s["report"].get("bytes", 0) for s in services),
        "published_action_rows": sum(len(s["report"].get("action_entries", [])) for s in services),
        "specific_action_rows": sum(sum(not a["none_identified"] for a in s["report"].get("action_entries", []))
                                  for s in services),
        "full_text_pages": sum(s["report"].get("pages", 0) for s in services),
        "errors": errors, "review": review, "passed": not errors,
    }
    write_json(ROOT / "validation.json", result)
    print(json.dumps({k: v for k, v in result.items() if k not in ["review", "rating_counts"]}, indent=2))
    print(f"Extraction review records: {len(review)}")
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
