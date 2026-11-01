"""Stage only the user-approved, contact-minimised CIW public release."""
import argparse
import hashlib
import json
import shutil
import sqlite3
from capture_branding import excluded_regulator_brand
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
DISALLOWED = {"phone", "email", "primaryTelephoneNumber", "primaryEmailAddress",
              "responsibleIndividuals", "responsible_individuals", "registeredPersons",
              "registered_persons"}


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def source_record(service):
    """Allow-listed public metadata rather than a copy of the contact-bearing source."""
    return {key: value for key, value in service.items() if key != "report"}


def public_database(data, path):
    if path.exists():
        path.unlink()
    with sqlite3.connect(path) as connection:
        connection.executescript("""
        PRAGMA foreign_keys=ON;
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE services (
          urn TEXT PRIMARY KEY,name TEXT,provider TEXT,local_authority TEXT,
          postcode TEXT,nursing TEXT,capacity INTEGER,source_url TEXT,metadata_json TEXT);
        CREATE TABLE ratings (
          service_urn TEXT REFERENCES services(urn),theme TEXT,rating TEXT,
          PRIMARY KEY(service_urn,theme));
        CREATE TABLE inspections (
          service_urn TEXT REFERENCES services(urn),inspection_urn TEXT,is_latest INTEGER,
          visit_start TEXT,visit_end TEXT,published_date TEXT,metadata_json TEXT,
          PRIMARY KEY(service_urn,inspection_urn));
        CREATE TABLE reports (
          service_urn TEXT PRIMARY KEY REFERENCES services(urn),status TEXT,file_path TEXT,
          source_url TEXT,sha256 TEXT,pages INTEGER,summary_kind TEXT,summary TEXT,
          evidence_json TEXT);
        CREATE TABLE report_pages (
          service_urn TEXT REFERENCES services(urn),page INTEGER,text TEXT,
          PRIMARY KEY(service_urn,page));
        CREATE TABLE findings (
          id INTEGER PRIMARY KEY,service_urn TEXT REFERENCES services(urn),page INTEGER,
          kind TEXT,evidence_text TEXT,topics_json TEXT);
        CREATE TABLE action_entries (
          id INTEGER PRIMARY KEY,service_urn TEXT REFERENCES services(urn),page INTEGER,
          category TEXT,regulation TEXT,evidence_text TEXT,status TEXT,date_identified TEXT,
          none_identified INTEGER,topics_json TEXT);
        CREATE TABLE topics (
          id TEXT PRIMARY KEY,name TEXT,suggested_action TEXT,owner TEXT,evidence TEXT);
        CREATE INDEX findings_service ON findings(service_urn);
        CREATE INDEX services_authority ON services(local_authority);
        """)
        for key, value in data["meta"].items():
            connection.execute("INSERT INTO metadata VALUES (?,?)", (key, json.dumps(value)))
        for topic in data["topics"]:
            connection.execute("INSERT INTO topics VALUES (?,?,?,?,?)",
                               (topic["id"], topic["name"], topic["action"], topic["owner"], topic["evidence"]))
        with sqlite3.connect(ROOT / "catalogue.sqlite") as original:
            for service in data["services"]:
                urn = service["urn"]
                connection.execute("INSERT INTO services VALUES (?,?,?,?,?,?,?,?,?)", (
                    urn, service["name"], service["provider"], service["local_authority"],
                    service["postcode"], service["nursing"], service["capacity"], service["source_url"],
                    json.dumps(source_record(service), ensure_ascii=False)))
                for theme, rating in service["ratings"].items():
                    connection.execute("INSERT INTO ratings VALUES (?,?,?)", (urn, theme, rating))
                inspections = [(service["inspection"], 1)] + [
                    (inspection, 0) for inspection in service["previous_inspections"]]
                for inspection, latest in inspections:
                    if not inspection:
                        continue
                    connection.execute("INSERT INTO inspections VALUES (?,?,?,?,?,?,?)", (
                        urn, inspection["urn"], latest, inspection.get("firstSuccessfulSiteVisit"),
                        inspection.get("lastSuccessfulSiteVisit"), inspection.get("reportPublishedActualDate"),
                        json.dumps(inspection)))
                report = service["report"]
                # Full source evidence remains reusable without duplicating the entire service JSON.
                connection.execute("INSERT INTO reports VALUES (?,?,?,?,?,?,?,?,?)", (
                    urn, report["status"], report.get("file"), report["source_url"], report.get("sha256"),
                    report.get("pages"), report.get("summary_kind"), report.get("summary"),
                    json.dumps({key: report[key] for key in [
                        "sections", "action_tables", "language_statement", "section_boundary_notes",
                        "table_review_pages", "improvement_text", "priority_status", "extraction_status"]
                        if key in report}, ensure_ascii=False)))
                for page, text in original.execute("SELECT page,text FROM report_pages WHERE service_urn=?", (urn,)):
                    connection.execute("INSERT INTO report_pages VALUES (?,?,?)", (urn, page, text))
                for finding in report.get("findings", []):
                    connection.execute("INSERT INTO findings(service_urn,page,kind,evidence_text,topics_json)"
                                       "VALUES (?,?,?,?,?)", (urn, finding["page"], finding["kind"],
                                                             finding["text"], json.dumps(finding["topics"])))
                for entry in report.get("action_entries", []):
                    connection.execute("INSERT INTO action_entries(service_urn,page,category,regulation,"
                                       "evidence_text,status,date_identified,none_identified,topics_json)"
                                       "VALUES (?,?,?,?,?,?,?,?,?)", (
                                           urn, entry["page"], entry["category"], entry["regulation"],
                                           entry["text"], entry["status"], entry["date_identified"],
                                           entry["none_identified"], json.dumps(entry["topics"])))
        connection.commit()
        connection.execute("VACUUM")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Public database integrity check failed.")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("Public database foreign keys are inconsistent.")
    if path.stat().st_size >= 100_000_000:
        raise RuntimeError("Public database exceeds safe GitHub per-file size; do not commit.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-database", action="store_true")
    args = parser.parse_args()
    data = json.loads((ROOT / "catalogue.json").read_text(encoding="utf-8"))
    if not data["meta"].get("complete") or data["meta"]["report_counts"].get("downloaded") != 980:
        raise RuntimeError("Refusing to publish an incomplete or unexpected capture.")
    data = deepcopy(data)
    branding_path = ROOT / "customer-branding.json"
    branding = json.loads(branding_path.read_text(encoding="utf-8")) if branding_path.exists() else {"services": {}, "meta": {}}
    for service in data["services"]:
        for key in DISALLOWED:
            service.pop(key, None)
        # Registration-condition strings may identify responsible individuals.
        service["conditions"] = [condition for condition in service["conditions"]
                                 if "responsible individual" not in condition.lower()]
        service["imposed_conditions"] = [condition for condition in service["imposed_conditions"]
                                         if "responsible individual" not in condition.lower()]
        source_brand = branding.get("services", {}).get(service["urn"])
        if source_brand:
            service["branding"] = {key: source_brand[key] for key in [
                "verified", "logo_status", "listed_website", "website_url", "source_url",
                "website_title", "checked_at", "website_reachable", "logo_url",
                "image_kind", "image_alt", "attribution"] if key in source_brand}
            if excluded_regulator_brand(service["branding"].get("logo_url"), service["branding"].get("image_alt", "")):
                service["branding"].pop("logo_url", None)
                service["branding"].pop("image_kind", None)
                service["branding"].update(verified=False, logo_status="regulator_logo_excluded")
    data["meta"].update(
        release_name="CareLens Cymru",
        public_release=True,
        github_repository="https://github.com/jonnychipz/carelens-cymru",
        site_url="https://jonnychipz.github.io/carelens-cymru/",
        public_scope="Public CIW inspection evidence and contact-minimised directory metadata. Original public inspection PDFs are reproduced unchanged.",
        database_note="The public SQLite schema is contact-minimised and differs from the private research archive; see PUBLIC-DATA.txt.")
    data["meta"]["branding"] = branding.get("meta", {})
    PUBLIC.mkdir(exist_ok=True)
    write_json(PUBLIC / "catalogue.json", data)
    write_json(PUBLIC / "coverage.json", data["meta"])
    for name in ["DATA-DICTIONARY.txt", "PUBLIC-DATA.txt", "README.txt", "LICENSE", "validation.json",
                 "guidance.json", "report-library.json"]:
        if (ROOT / name).exists():
            shutil.copy2(ROOT / name, PUBLIC / name)
    for name in ["reports", "text", "guidance", "assets", "management-reports"]:
        if (ROOT / name).is_dir():
            ignored = shutil.ignore_patterns("ratings-source-page.html") if name == "guidance" else None
            shutil.copytree(ROOT / name, PUBLIC / name, dirs_exist_ok=True, ignore=ignored)
    copied_source_page = PUBLIC / "guidance" / "ratings-source-page.html"
    if copied_source_page.exists():
        copied_source_page.unlink()
    if not args.skip_database:
        public_database(data, PUBLIC / "catalogue.sqlite")
    (PUBLIC / ".nojekyll").write_text("", encoding="utf-8")
    manifest = []
    for path in sorted(PUBLIC.rglob("*")):
        if not path.is_file() or path.name in ["release-manifest.json", "index.html"]:
            continue
        size = path.stat().st_size
        if size >= 100_000_000:
            raise RuntimeError(f"Release file exceeds safe GitHub size: {path.name}")
        manifest.append({"path": path.relative_to(PUBLIC).as_posix(), "bytes": size,
                         "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    write_json(PUBLIC / "release-manifest.json", {
        "name": "CareLens Cymru", "snapshot_date": data["meta"]["snapshot_date"],
        "inventory_services": len(data["services"]), "files": manifest,
        "scope": data["meta"]["public_scope"]})
    print(json.dumps({"staged_services": len(data["services"]),
                      "staged_files": len(manifest),
                      "bytes": sum(item["bytes"] for item in manifest),
                      "public_database_bytes": (PUBLIC / "catalogue.sqlite").stat().st_size
                      if (PUBLIC / "catalogue.sqlite").exists() else None}))


if __name__ == "__main__":
    main()
