"""Verify the approved public release before it is committed or deployed."""
import hashlib
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

import fitz
from capture_branding import excluded_regulator_brand

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
FORBIDDEN = {"phone", "email", "primaryTelephoneNumber", "primaryEmailAddress",
             "responsibleIndividuals", "responsible_individuals", "registeredPersons"}


def main():
    errors = []
    data = json.loads((PUBLIC / "catalogue.json").read_text(encoding="utf-8"))
    private = json.loads((ROOT / "catalogue.json").read_text(encoding="utf-8"))
    if not data["meta"].get("public_release"):
        errors.append("Release is not explicitly marked public.")
    if {s["urn"] for s in data["services"]} != {s["urn"] for s in private["services"]}:
        errors.append("Public and source inventories do not match.")
    for service in data["services"]:
        brand = service.get("branding", {})
        if excluded_regulator_brand(brand.get("logo_url"), brand.get("image_alt", "")):
            errors.append(f"{service['urn']}: excluded regulator logo reference remains.")
        if FORBIDDEN & service.keys():
            errors.append(f"{service['urn']}: excluded directory fields remain.")
        if any("responsible individual" in c.lower() for c in service["conditions"]):
            errors.append(f"{service['urn']}: excluded responsible-individual condition remains.")
        report = service["report"]
        if report["status"] != "downloaded":
            continue
        path = PUBLIC / report["file"]
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != report["sha256"]:
            errors.append(f"{service['urn']}: published PDF is missing or altered.")
    if (PUBLIC / "source").exists():
        errors.append("Raw private directory source must not be staged.")
    if (PUBLIC / "guidance" / "ratings-source-page.html").exists():
        errors.append("Unneeded third-party-script-bearing source page must not be staged.")
    with sqlite3.connect(PUBLIC / "catalogue.sqlite") as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            errors.append("Public database integrity failed.")
        if db.execute("SELECT COUNT(*) FROM services").fetchone()[0] != len(data["services"]):
            errors.append("Public database service count mismatch.")
        if db.execute("SELECT COUNT(*) FROM report_pages").fetchone()[0] != 9126:
            errors.append("Public database page count mismatch.")
        columns = {row[1] for row in db.execute("PRAGMA table_info(services)")}
        if FORBIDDEN & columns:
            errors.append("Public database contains excluded service columns.")
        for urn, text in db.execute("SELECT urn,metadata_json FROM services"):
            if FORBIDDEN & json.loads(text).keys():
                errors.append(f"{urn}: public database metadata includes an excluded field.")
    library = json.loads((PUBLIC / "report-library.json").read_text(encoding="utf-8"))
    reports = library["reports"]
    baseline = next(r for r in reports if r["id"] == "baseline")
    if set(baseline["service_urns"]) != {s["urn"] for s in data["services"]}:
        errors.append("Baseline cohort is not the full service inventory.")
    monthly_urns = Counter(urn for report in reports if report["kind"] == "monthly"
                           for urn in report["service_urns"])
    expected = {s["urn"] for s in data["services"] if s["report"]["status"] == "downloaded"}
    if set(monthly_urns) != expected or any(value != 1 for value in monthly_urns.values()):
        errors.append("Monthly report cohorts must cover every archived report exactly once.")
    service_lookup = {s["urn"]: s for s in data["services"]}
    for report in reports:
        if report["kind"] == "monthly":
            for urn in report["service_urns"]:
                published = service_lookup[urn]["inspection"]["reportPublishedActualDate"][:7]
                if published != report["month"]:
                    errors.append(f"{report['id']}: a service is not in its publication-month cohort.")
        for key in ["pdf", "html"]:
            path = PUBLIC / report[key]
            if not path.is_file():
                errors.append(f"{report['id']}: {key} report file missing.")
            elif key == "pdf":
                with fitz.open(path) as pdf:
                    if not len(pdf) or not any("CareLens" in p.get_text() for p in pdf):
                        errors.append(f"{report['id']}: empty or unidentified PDF.")
    html = (PUBLIC / "index.html").read_text(encoding="utf-8")
    if "CareLens Cymru" not in html:
        errors.append("Public application branding missing.")
    if re.search(r'<script[^>]+src=|<link[^>]+href=["\']https?://|@import\s', html, re.I):
        errors.append("Application unexpectedly relies on external resources.")
    if "__CARELENS_REPORTS__" in html or "__CIW_DATA__" in html:
        errors.append("Application contains unreplaced build markers.")
    match = re.search(r'<script[^>]+id=["\']ciw-data["\'][^>]*>(.*?)</script>', html, re.S)
    embedded = json.loads(match[1]) if match else None
    if not embedded or len(embedded["services"]) != len(data["services"]):
        errors.append("Embedded application corpus mismatch.")
    elif any(FORBIDDEN & service.keys() for service in embedded["services"]):
        errors.append("Embedded application still contains excluded directory fields.")
    report_match = re.search(r'<script[^>]+id=["\']carelens-reports["\'][^>]*>(.*?)</script>', html, re.S)
    embedded_reports = json.loads(report_match[1]) if report_match else None
    if not embedded_reports or {r["id"] for r in embedded_reports["reports"]} != {r["id"] for r in reports}:
        errors.append("Embedded report library differs from downloadable reports.")
    if "My action plan" in html:
        errors.append("Superseded action-plan navigation remains in the public site.")
    files = [p for p in PUBLIC.rglob("*") if p.is_file()]
    if any(p.stat().st_size >= 100_000_000 for p in files):
        errors.append("A release file exceeds safe GitHub size.")
    result = {
        "name": "CareLens Cymru", "services": len(data["services"]),
        "archived_pdfs": len(expected), "manager_reports": len(reports),
        "monthly_reports": sum(r["kind"] == "monthly" for r in reports),
        "monthly_cohort_total": sum(monthly_urns.values()),
        "public_bytes": sum(p.stat().st_size for p in files),
        "public_files": len(files), "errors": errors, "passed": not errors,
    }
    (ROOT / "release-validation.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
