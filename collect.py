"""Resumable public CIW directory snapshot, PDF archive and evidence catalogue."""
import argparse
import hashlib
import json
import math
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

import fitz
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent
BASE = "https://digital.careinspectorate.wales"
FILTER = "ad283d57-720b-46b0-a7a0-a0ee8ba29cc4"
SNAPSHOT_DATE = "2026-11-01"
THEMES = ["Well-being", "Care and Support", "Environment", "Leadership and Management"]
TOPICS = [
    {"id": "medicines", "name": "Medicines management", "pattern": r"\bmedicin|\bmedication|\bMAR\b",
     "action": "Audit medicine administration records, storage, reconciliation, PRN protocols and staff competency; document exceptions and follow-up.", "owner": "Clinical lead / medicines lead", "evidence": "MAR audit, competency records, storage checks and documented corrective actions"},
    {"id": "safeguarding", "name": "Safeguarding & incident learning", "pattern": r"safeguard|abuse|\bincident|\bnotification",
     "action": "Review safeguarding escalation, incident reporting and CIW notifications. Check that staff understand local procedures and that learning reaches the team.", "owner": "Registered manager", "evidence": "Escalation log, training records, incident reviews and notifications"},
    {"id": "care-plans", "name": "Personal plans & risk assessment", "pattern": r"personal plan|care plan|risk assess|care document",
     "action": "Sample personal plans for individual preferences, changing needs, review dates and usable risk controls. Check that staff can apply the guidance in practice.", "owner": "Care lead", "evidence": "Plan reviews, resident involvement and observed practice"},
    {"id": "staffing", "name": "Staffing & recruitment", "pattern": r"staffing|recruit|staff levels|staff numbers|agency staff|\bDBS\b|turnover|induction",
     "action": "Review staffing against actual dependency and safe recruitment checks. Examine continuity, induction and the impact of vacancies or agency use.", "owner": "Registered manager / HR", "evidence": "Dependency reviews, rota analysis and recruitment checks"},
    {"id": "training", "name": "Training & supervision", "pattern": r"training|supervision|competenc|appraisal",
     "action": "Reconcile the training matrix with current roles and residents' needs. Sample supervision records and assess competence, rather than attendance alone.", "owner": "Training lead", "evidence": "Training matrix, competency assessment and supervision records"},
    {"id": "governance", "name": "Leadership & quality assurance", "pattern": r"quality assurance|quality of|responsible individual|\bRI\b|\baudit|oversight|governance|leadership|managers?|regulatory",
     "action": "Review responsible-individual oversight and quality reviews. Track audit actions to closure and show how resident and relative feedback changes the service.", "owner": "Responsible individual / manager", "evidence": "RI visit records, quality reports, feedback and closed action log"},
    {"id": "rights", "name": "Rights, choice & independence", "pattern": r"independen|choice|dignity|best interest|deprivation|DoLS|capacity|consent",
     "action": "Check consent and decision-making records, legal authorisations where applicable, and how daily practice supports choice, dignity and independence.", "owner": "Care lead", "evidence": "Decision records, authorisations and resident-led goals"},
    {"id": "nutrition", "name": "Nutrition & hydration", "pattern": r"nutrition|hydration|\bdiet\b|weight|meals|food|fluid",
     "action": "Review nutritional and hydration risks, weight trends, meal preferences and escalation when needs change; assess the dining experience with residents.", "owner": "Care lead / catering lead", "evidence": "Risk screening, monitoring, referrals and resident feedback"},
    {"id": "infection", "name": "Infection prevention", "pattern": r"infection|hygiene|cleanliness|cleaning",
     "action": "Check infection-control practice, cleaning assurance and staff competence. Follow up audit exceptions and verify that practical supplies and routines are effective.", "owner": "Infection prevention lead", "evidence": "IPC audit, cleaning checks and competency records"},
    {"id": "environment", "name": "Environment & fire safety", "pattern": r"fire|maintenance|maintained|environment|premises|equipment|health and safety",
     "action": "Review fire and premises safety, servicing, accessibility and environmental risks; verify outstanding maintenance actions and person-specific evacuation arrangements.", "owner": "Estates lead / manager", "evidence": "Safety checks, servicing, maintenance log and evacuation plans"},
    {"id": "welsh", "name": "Welsh language & culture", "pattern": r"Welsh|active offer|language and culture",
     "action": "Assess the Welsh language active offer and cultural needs proactively, including how preferences are identified and acted upon; do not equate no requests with adequate provision.", "owner": "Registered manager", "evidence": "Language preferences, active-offer arrangements and accessible information"},
    {"id": "activities", "name": "Meaningful activity & community", "pattern": r"activit|community|social|relationships|visitors",
     "action": "Review opportunities for personally meaningful activity, relationships and community participation, using resident outcomes rather than activity counts alone.", "owner": "Well-being lead", "evidence": "Resident goals, participation records and feedback"},
]
COMPILED = [(t["id"], re.compile(t["pattern"], re.I)) for t in TOPICS]


def write_json(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def english(value):
    return (value or {}).get("englishText", "")


def make_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET", "POST"], respect_retry_after_header=True)
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": "CIW-public-research-catalogue/1.0",
                            "Accept-Language": "en-GB,en;q=0.9"})
    response = session.get(BASE + "/directory/search?fromCiwWebsite=1", timeout=60)
    response.raise_for_status()
    token = session.cookies.get("XSRF-TOKEN")
    if not token:
        raise RuntimeError("Public directory did not issue its normal anti-forgery token.")
    session.headers.update({"X-XSRF-TOKEN": unquote(token), "Referer": response.url})
    return session


def capture_directory(session):
    target = ROOT / "source" / "directory.json"
    if target.exists():
        saved = json.loads(target.read_text(encoding="utf-8"))
        if saved["filter_id"] != FILTER:
            raise RuntimeError("Existing snapshot has a different care-type filter.")
        return saved
    body = {k: "" for k in [
        "localAuthority", "operatingArea", "keywords", "postcode", "childcareAndPlaySubTypes",
        "openDuring", "mainLanguage", "serviceNeeds", "serviceRatings", "providerServiceTypes",
        "flyingStartOnly", "registeredToDeliverChildcareOffer", "deliveringEarlyYearsEducation"]}
    body.update(currentPage=1, serviceType=FILTER, distance="5")
    services, expected, page_count = [], None, None
    page = 1
    while True:
        body["currentPage"] = page
        response = session.post(BASE + "/backend/Directory/GetSearchResults", json=body, timeout=60)
        response.raise_for_status()
        result = response.json()
        write_json(ROOT / "source" / f"search-page-{page:03d}.json", result)
        if expected is None:
            expected = result["totalRecordCount"]
            if not result["services"]:
                raise RuntimeError("Filtered directory returned no services.")
            page_count = math.ceil(expected / len(result["services"]))
        if result["totalRecordCount"] != expected:
            raise RuntimeError("Directory count changed during capture; refusing an incomplete snapshot.")
        services.extend(result["services"])
        if page % 10 == 0 or page == page_count:
            print(f"Directory: page {page}/{page_count}, {len(services)}/{expected} services", flush=True)
        if page >= page_count:
            break
        page += 1
        time.sleep(0.12)
    if len(services) != expected or len({s["urn"] for s in services}) != expected:
        raise RuntimeError("Directory count/unique URN completeness check failed.")
    if any(english(s.get("serviceTypeSticker")) != "Care Homes for Adults" for s in services):
        raise RuntimeError("A non-adult-care-home result entered the snapshot.")
    snapshot = {"captured_at": datetime.now(timezone.utc).isoformat(), "snapshot_date": SNAPSHOT_DATE,
                "filter_id": FILTER, "filter_label": "Care Homes for Adults",
                "source_url": BASE + "/directory/search?fromCiwWebsite=1",
                "total": expected, "pages": page_count, "services": services}
    write_json(target, snapshot)
    return snapshot


def clean(text):
    text = re.sub(r"(?im)^\s*Page\s+\d+\s+of\s+\d+\s*$", "", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def topic_ids(text):
    return [key for key, pattern in COMPILED if pattern.search(text)]


HEADING = re.compile(
    r"^\s*(Summary|Well[- ]?being|Care\s*(?:&|and)\s*Support|Environment|"
    r"Leadership\s*(?:&|and)\s*Management|Areas identified for improvement|"
    r"Improvements required and recommended following this inspection|"
    r"Areas for improvement and action|Areas for improvement|Summary of Non-Compliance)\s*:?"
    r"(?:\s+(?:Excellent|Good|Requires Improvement|Requires Significant Improvement))?\s*$", re.I)


def canonical_heading(value):
    low = value.lower()
    if low == "summary":
        return "Summary"
    if "improvement" in low or "non-compliance" in low:
        return "Improvement and action"
    if low.startswith("well"):
        return THEMES[0]
    if low.startswith("care"):
        return THEMES[1]
    if low.startswith("environment"):
        return THEMES[2]
    return THEMES[3]


def extract_report(pdf, urn):
    corrections_path = ROOT / "source" / "section-boundary-corrections.json"
    corrections = (json.loads(corrections_path.read_text(encoding="utf-8")).get(urn, [])
                   if corrections_path.exists() else [])
    with fitz.open(pdf) as document:
        pages = [{"page": i + 1, "text": page.get_text(sort=True)} for i, page in enumerate(document)]
        action_tables = []
        action_entries = []
        table_review_pages = []
        action_region = False
        remembered_category, remembered_layout = None, None
        for index, page in enumerate(document):
            text = pages[index]["text"]
            heading_found = bool(re.search(r"Areas identified for improvement|Summary of Non-Compliance", text, re.I))
            if heading_found:
                action_region = True
                remembered_category, remembered_layout = None, None
            elif re.search(r"Crown copyright|Was this report helpful", text, re.I):
                action_region = False
            if not action_region:
                continue
            tables = page.find_tables().tables
            if not tables and re.search(r"table\(s\) below|Summary of Non-Compliance", text, re.I):
                table_review_pages.append(index + 1)
            for table in tables:
                rows = table.extract()
                action_tables.append({"page": index + 1, "rows": rows})
                category, layout = remembered_category, remembered_layout
                for row in rows:
                    cells = [re.sub(r"\s+", " ", cell or "").strip() for cell in row]
                    first = cells[0] if cells else ""
                    if re.search(r"Priority Action Notice|Summary of.*Priority", first, re.I):
                        category, layout = "Priority action", "legacy" if len(cells) >= 3 else "current"
                        remembered_category, remembered_layout = category, layout
                        continue
                    if re.search(r"Area\(s\) for Improvement|Summary of Areas for Improvement", first, re.I):
                        category, layout = "Area for improvement", "legacy" if len(cells) >= 3 else "current"
                        remembered_category, remembered_layout = category, layout
                        continue
                    if re.search(r"Summary of Non-Compliance|What each means|Where we identify|Where we find|"
                                 r"The table\(s\) below|Where providers|We respond to|We expect the provider", first, re.I):
                        continue
                    if not category or first.lower() in ["regulation", "summary"] or not first:
                        continue
                    if layout == "legacy" and len(cells) >= 3:
                        entry = {"category": category, "regulation": cells[0], "text": cells[1],
                                 "status": cells[2], "date_identified": None, "page": index + 1}
                    elif layout == "current" and len(cells) >= 2:
                        entry = {"category": category, "regulation": None, "text": cells[0],
                                 "status": None, "date_identified": cells[1], "page": index + 1}
                    else:
                        table_review_pages.append(index + 1)
                        continue
                    entry["none_identified"] = bool(re.search(r"No non-compliance of this type", entry["text"], re.I))
                    entry["topics"] = topic_ids(entry["text"]) if not entry["none_identified"] else []
                    action_entries.append(entry)
    unique_entries = {}
    for entry in action_entries:
        key = (entry["category"], entry["regulation"], entry["text"], entry["status"], entry["date_identified"])
        unique_entries.setdefault(key, entry)
    action_entries = list(unique_entries.values())
    full_text = "\n\n".join(f"--- PAGE {p['page']} ---\n{p['text']}" for p in pages)
    (ROOT / "text" / f"{urn}.txt").write_text(full_text, encoding="utf-8")
    total_chars = sum(len(p["text"].strip()) for p in pages)
    if total_chars < 250:
        return {"pages": len(pages), "extraction_status": "needs_ocr", "summary": "",
                "summary_source_pages": [], "sections": [], "findings": [], "topics": [],
                "improvement_text": "", "priority_status": "Not assessed",
                "language_statement": [], "action_entries": action_entries,
                "action_tables": action_tables, "table_review_pages": table_review_pages, "_pages": pages}
    sections, current, buffer, page_numbers = [], None, [], []

    def flush():
        nonlocal buffer, page_numbers
        if current and buffer:
            text = clean("\n".join(buffer))
            # Rating cells on early pages are not findings.
            if len(text) > 90:
                sections.append({"theme": current, "text": text, "pages": sorted(set(page_numbers))})
        buffer, page_numbers = [], []

    for page in pages:
        correction = next((c for c in corrections if c["page"] == page["page"]), None)
        if correction:
            flush()
            current = correction["theme"]
        for line in clean(page["text"]).splitlines():
            if re.match(r"^\s*Findings\s*:\s*$", line, re.I):
                flush()
                current = None
                continue
            if re.match(r"^\s*Was this report helpful\?", line, re.I):
                flush()
                current = None
                continue
            match = HEADING.match(line)
            if match and current == "Summary" and line.strip()[0].islower():
                # A wrapped prose word such as "well-being" is not a theme heading.
                match = None
            if match:
                flush()
                current = canonical_heading(match.group(1))
                continue
            if re.search(r"Crown copyright|Open Government Licen|Mae.r adroddiad", line, re.I):
                flush()
                current = None
            if current:
                buffer.append(line)
                if line.strip():
                    page_numbers.append(page["page"])
    flush()
    sections = list({(s["theme"], s["text"]): s for s in sections}.values())
    summaries = [s for s in sections if s["theme"] == "Summary"]
    summary = "\n\n".join(s["text"] for s in summaries)
    summary_pages = sorted({p for s in summaries for p in s["pages"]})
    summary_kind = "Official CIW summary"
    if not summary:
        excerpts = []
        excerpt_pages = []
        for section in sections:
            if section["theme"] not in THEMES:
                continue
            sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", re.sub(r"\s+", " ", section["text"]))
            excerpts.append(section["theme"] + ": " + " ".join(sentences[:2]))
            excerpt_pages.append(section["pages"][0])
        if excerpts:
            summary = "\n\n".join(excerpts)
            summary_pages = sorted(set(excerpt_pages))
            summary_kind = "Theme-based extracts - official summary blank or absent"
    findings = []
    language_statements = []
    # Extractive evidence retains the original page; labels describe wording, not a regulatory judgement.
    for page in pages:
        text = clean(page["text"])
        if re.search(r"Crown copyright|Open Government Licen", text, re.I) and len(text) < 1800:
            continue
        if re.search(r"Areas identified for improvement|Summary of Non-Compliance", text, re.I):
            # Extract specific action table rows instead of boilerplate describing CIW's powers.
            continue
        text = "\n".join(line for line in text.splitlines()
                         if not HEADING.match(line) and not re.match(r"^\s*Findings\s*:", line, re.I))
        chunks = re.split(r"(?<=[.!?])\s+(?=[A-Z])", re.sub(r"\s+", " ", text))
        for chunk in chunks:
            if not 55 <= len(chunk) <= 1100:
                continue
            topics = topic_ids(chunk)
            if not topics:
                continue
            if any(t in chunk.lower() for t in [
                    "where we identify", "where we find", "we expect the provider",
                    "in these circumstances", "we will inspect again", "you may use and re-use",
                    "you must reproduce", "this report is also available"]):
                continue
            explicit_improvement = bool(re.search(
                r"requires? improvement|must (?:ensure|improve|provide|review|take)|needs? to "
                r"(?:ensure|improve|review|develop)|not (?:meeting|sufficient|consistently|always|in place)|"
                r"not been (?:completed|updated|reviewed)|were not|was not|failed to", chunk, re.I))
            positive = bool(re.search(r"\bgood\b|effective|robust|positive|well supported|"
                                      r"recruited safely|safe and|high standard", chunk, re.I))
            kind = "Improvement wording" if explicit_improvement else (
                "Positive wording" if positive else "Inspection evidence")
            findings.append({"text": chunk, "page": page["page"], "topics": topics, "kind": kind})
            if "welsh" in topics and re.search(r"language.*(?:needs|offer)|active offer", chunk, re.I):
                language_statements.append({"text": chunk, "page": page["page"]})
    for entry in action_entries:
        if entry["none_identified"]:
            continue
        findings.append({"text": entry["text"], "page": entry["page"], "topics": entry["topics"],
                         "kind": "Published action table", "action_category": entry["category"],
                         "action_status": entry["status"], "date_identified": entry["date_identified"]})
    improvements = "\n\n".join(s["text"] for s in sections if s["theme"] == "Improvement and action")
    flat = re.sub(r"\s+", " ", "\n".join(p["text"] for p in pages))
    no_priority = bool(re.search(r"(?:has not issued any|no)\s+priority action notices?", flat, re.I))
    no_priority = no_priority or any(e["category"] == "Priority action" and e["none_identified"]
                                    for e in action_entries)
    priority = "Explicitly none issued" if no_priority else "Review action section"
    if not no_priority and re.search(r"(?:have|has|we)\s+issued (?:a |an |[a-z]+ )?priority action", flat, re.I):
        priority = "Issue wording found - verify"
    if not no_priority and any(e["category"] == "Priority action" and not e["none_identified"]
                               for e in action_entries):
        priority = "Published priority action table - check dates/status"
    for section in sections:
        section["topics"] = topic_ids(section["text"])
    return {"pages": len(pages), "extraction_status": (
                "extracted" if summary and summary_kind == "Official CIW summary" else "summary_needs_review"),
            "summary": summary, "summary_kind": summary_kind,
            "summary_source_pages": summary_pages, "sections": sections,
            "findings": findings, "topics": sorted({t for f in findings for t in f["topics"]}),
            "improvement_text": improvements, "priority_status": priority,
            "language_statement": language_statements, "action_entries": action_entries,
            "action_tables": action_tables, "table_review_pages": sorted(set(table_review_pages)),
            "section_boundary_notes": corrections,
            "_pages": pages}


def normalise(service):
    address = service.get("address") or {}
    inspection = service.get("latestInspection")
    ratings = {}
    published_records = (service.get("ratings") or []) if service.get("showRatings") else []
    for record in published_records:
        theme, result = english(record["theme"]), english(record["result"])
        if theme in ratings and ratings[theme] != result:
            raise ValueError(f"{service['urn']}: conflicting public ratings for {theme}")
        ratings[theme] = result
    source = BASE + "/directory/service/" + service["urn"]
    return {
        "urn": service["urn"], "name": service["name"], "provider": service.get("providerName", ""),
        "provider_urn": service.get("providerURN", ""), "source_url": source,
        "address": ", ".join(address[k] for k in ["line1", "line2", "line3", "town", "postcode"]
                             if address.get(k)),
        "town": address.get("town", ""), "postcode": address.get("postcode", ""),
        "latitude": address.get("latitude"), "longitude": address.get("longitude"),
        "local_authority": english(service.get("localAuthority")) if service.get("showLocalAuthority") else "",
        "phone": service.get("primaryTelephoneNumber", "") if service.get("showTelephoneNumber") else "",
        "email": service.get("primaryEmailAddress", "") if service.get("showEmailAddress") else "",
        "website": service.get("websiteUrl", ""), "language": service.get("mainOperatingLanguage", ""),
        "nursing": service.get("nursingCareProvided") if service.get("showNursingCareProvided") else None,
        "capacity": service.get("maximumCapacity"),
        "capacity_publicly_displayed": service.get("showMaximumCapacityOnFrontEnd", False),
        "service_subtype": english(service.get("serviceSubType")),
        "needs": [english(n) for n in service.get("serviceNeeds") or []],
        "responsible_individuals": service.get("responsibleIndividuals") or [],
        "registration_date": service.get("dateOfApproval"), "modified_at": service.get("dateModified"),
        "conditions": [english(c) for c in service.get("standardConditions") or []],
        "imposed_conditions": [english(c) for c in service.get("imposedConditions") or []],
        "ratings": ratings, "duplicate_rating_records": len(published_records) - len(ratings),
        "inspection": inspection,
        "previous_inspections": service.get("previousInspections") or [],
        "report": {"status": "not_published", "source_url": source, "file": None}
    }


def init_database():
    connection = sqlite3.connect(ROOT / "catalogue.sqlite")
    connection.executescript("""
    PRAGMA foreign_keys=ON;
    CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, captured_at TEXT, filter_id TEXT,
      filter_label TEXT, expected_services INTEGER, metadata_json TEXT);
    CREATE TABLE IF NOT EXISTS services (
      urn TEXT PRIMARY KEY, name TEXT, provider TEXT, local_authority TEXT, postcode TEXT,
      nursing TEXT, capacity INTEGER, registration_date TEXT, source_url TEXT,
      raw_json TEXT NOT NULL, catalogue_json TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS ratings (
      service_urn TEXT REFERENCES services(urn), theme TEXT, rating TEXT,
      PRIMARY KEY(service_urn,theme));
    CREATE TABLE IF NOT EXISTS inspections (
      service_urn TEXT REFERENCES services(urn), inspection_urn TEXT,
      is_latest INTEGER, visit_start TEXT, visit_end TEXT, published_date TEXT, raw_json TEXT,
      PRIMARY KEY(service_urn,inspection_urn));
    CREATE TABLE IF NOT EXISTS reports (
      service_urn TEXT PRIMARY KEY REFERENCES services(urn), status TEXT, file_path TEXT,
      source_url TEXT, sha256 TEXT, bytes INTEGER, pages INTEGER, language TEXT,
      extraction_status TEXT, summary TEXT, improvement_text TEXT, priority_status TEXT,
      error TEXT, retrieved_at TEXT);
    CREATE TABLE IF NOT EXISTS report_pages (
      service_urn TEXT REFERENCES services(urn), page INTEGER, text TEXT,
      PRIMARY KEY(service_urn,page));
    CREATE TABLE IF NOT EXISTS findings (
      id INTEGER PRIMARY KEY, service_urn TEXT REFERENCES services(urn), page INTEGER,
      kind TEXT, evidence_text TEXT, topics_json TEXT);
    CREATE TABLE IF NOT EXISTS topics (
      id TEXT PRIMARY KEY, name TEXT, suggested_action TEXT, owner TEXT, evidence TEXT);
    CREATE TABLE IF NOT EXISTS action_entries (
      id INTEGER PRIMARY KEY, service_urn TEXT REFERENCES services(urn), page INTEGER,
      category TEXT, regulation TEXT, evidence_text TEXT, status TEXT,
      date_identified TEXT, none_identified INTEGER, topics_json TEXT);
    CREATE INDEX IF NOT EXISTS findings_service ON findings(service_urn);
    CREATE INDEX IF NOT EXISTS services_authority ON services(local_authority);
    """)
    for topic in TOPICS:
        connection.execute("INSERT OR REPLACE INTO topics VALUES (?,?,?,?,?)",
                           (topic["id"], topic["name"], topic["action"], topic["owner"], topic["evidence"]))
    connection.commit()
    return connection


def save_service(connection, source, service, pages=None):
    connection.execute("INSERT OR REPLACE INTO services VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
        service["urn"], service["name"], service["provider"], service["local_authority"],
        service["postcode"], service["nursing"], service["capacity"], service["registration_date"],
        service["source_url"], json.dumps(source, ensure_ascii=False), json.dumps(service, ensure_ascii=False)))
    connection.execute("DELETE FROM ratings WHERE service_urn=?", (service["urn"],))
    for theme, rating in service["ratings"].items():
        connection.execute("INSERT INTO ratings VALUES (?,?,?)", (service["urn"], theme, rating))
    for inspection, latest in [(service["inspection"], 1)] + [(i, 0) for i in service["previous_inspections"]]:
        if inspection:
            connection.execute("INSERT OR REPLACE INTO inspections VALUES (?,?,?,?,?,?,?)", (
                service["urn"], inspection["urn"], latest, inspection.get("firstSuccessfulSiteVisit"),
                inspection.get("lastSuccessfulSiteVisit"), inspection.get("reportPublishedActualDate"),
                json.dumps(inspection)))
    report = service["report"]
    connection.execute("INSERT OR REPLACE INTO reports VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        service["urn"], report["status"], report.get("file"), report.get("source_url"),
        report.get("sha256"), report.get("bytes"), report.get("pages"), report.get("language"),
        report.get("extraction_status"), report.get("summary"), report.get("improvement_text"),
        report.get("priority_status"), report.get("error"), report.get("retrieved_at")))
    if pages is not None:
        connection.execute("DELETE FROM report_pages WHERE service_urn=?", (service["urn"],))
        for page in pages:
            connection.execute("INSERT INTO report_pages VALUES (?,?,?)",
                               (service["urn"], page["page"], page["text"]))
    connection.execute("DELETE FROM findings WHERE service_urn=?", (service["urn"],))
    for finding in report.get("findings", []):
        connection.execute("INSERT INTO findings(service_urn,page,kind,evidence_text,topics_json) "
                           "VALUES (?,?,?,?,?)", (service["urn"], finding["page"], finding["kind"],
                                                 finding["text"], json.dumps(finding["topics"])))
    connection.execute("DELETE FROM action_entries WHERE service_urn=?", (service["urn"],))
    for entry in report.get("action_entries", []):
        connection.execute("INSERT INTO action_entries(service_urn,page,category,regulation,evidence_text,"
                           "status,date_identified,none_identified,topics_json) VALUES (?,?,?,?,?,?,?,?,?)", (
                               service["urn"], entry["page"], entry["category"], entry["regulation"],
                               entry["text"], entry["status"], entry["date_identified"],
                               entry["none_identified"], json.dumps(entry["topics"])))
    connection.commit()


def download_report(session, source, service, reextract=False):
    urn = service["urn"]
    inspection = service["inspection"]
    if not inspection:
        return []
    details = inspection.get("fileDetails") or {}
    language = "Welsh" if details.get("welshFileAvailableOnly") else "English"
    url = BASE + f"/backend/directory/service/{urn}/LatestInspectionReport"
    filename = f"reports/{urn}-{(inspection.get('lastSuccessfulSiteVisit') or 'undated')[:10]}-{'CY' if language == 'Welsh' else 'EN'}.pdf"
    path = ROOT / filename
    result_path = ROOT / "source" / f"{urn}-analysis.json"
    report = {"status": "pending", "file": filename, "source_url": url, "language": language,
              "retrieved_at": datetime.now(timezone.utc).isoformat()}
    if result_path.exists() and path.exists():
        previous = json.loads(result_path.read_text(encoding="utf-8"))
        if previous.get("sha256") and previous["sha256"] != hashlib.sha256(path.read_bytes()).hexdigest():
            raise RuntimeError(f"{urn}: cached PDF SHA-256 mismatch; preserve and investigate before retrying.")
        if previous.get("sha256") == hashlib.sha256(path.read_bytes()).hexdigest():
            if not reextract:
                service["report"] = previous
                return None
            report["retrieved_at"] = previous.get("retrieved_at")
            report["content_disposition"] = previous.get("content_disposition")
    try:
        if not path.exists():
            # The English directory supplies English reports; Welsh-only availability is recorded explicitly.
            response = session.get(url, timeout=(15, 90),
                                   headers={"Accept-Language": "cy" if language == "Welsh" else "en"})
            response.raise_for_status()
            if not response.content.startswith(b"%PDF"):
                raise ValueError(f"Latest-report endpoint returned non-PDF content: {response.headers.get('Content-Type')}")
            temporary = path.with_suffix(".pdf.part")
            temporary.write_bytes(response.content)
            with fitz.open(temporary) as document:
                if len(document) == 0 or document.is_encrypted:
                    raise ValueError("Empty or encrypted report")
            temporary.replace(path)
            report["content_disposition"] = response.headers.get("Content-Disposition")
        content = path.read_bytes()
        report.update(status="downloaded", bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
        extracted = extract_report(path, urn)
        pages = extracted.pop("_pages")
        report.update(extracted)
        report["analysed_at"] = datetime.now(timezone.utc).isoformat()
        write_json(result_path, report)
        service["report"] = report
        return pages
    except (requests.RequestException, ValueError, fitz.FileDataError, RuntimeError) as error:
        report.update(status="extraction_failed" if path.exists() else "download_failed",
                      error=f"{type(error).__name__}: {error}")
        service["report"] = report
        write_json(result_path, report)
        print(f"ERROR {urn}: {report['error']}", flush=True)
        return []


def export_catalogue(connection, snapshot):
    services = [json.loads(row[0]) for row in connection.execute(
        "SELECT catalogue_json FROM services ORDER BY name COLLATE NOCASE")]
    counts = {}
    extraction_counts = {}
    for service in services:
        report = service["report"]
        counts[report["status"]] = counts.get(report["status"], 0) + 1
        if report.get("extraction_status"):
            key = report["extraction_status"]
            extraction_counts[key] = extraction_counts.get(key, 0) + 1
    metadata = {k: v for k, v in snapshot.items() if k != "services"}
    metadata.update(processed=len(services), report_counts=counts, extraction_counts=extraction_counts,
                    updated_at=datetime.now(timezone.utc).isoformat(),
                    complete=len(services) == snapshot["total"] and not counts.get("pending"),
                    rating_note="CIW publishes separate theme ratings; no overall rating is invented. Missing ratings are not a judgement of quality.",
                    summary_note="Official summary extracts and page-linked automated evidence selection. Wording labels are search aids, not independently verified findings or regulatory conclusions.",
                    licence="CIW / Welsh Government, Crown copyright. Report reuse under the Open Government Licence where stated; logos excluded.",
                    licence_url="https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/")
    metadata["duplicate_rating_services"] = sum(bool(s.get("duplicate_rating_records")) for s in services)
    data = {"meta": metadata, "themes": THEMES, "topics": TOPICS, "services": services}
    write_json(ROOT / "catalogue.json", data)
    write_json(ROOT / "coverage.json", metadata)
    return data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reextract", action="store_true")
    parser.add_argument("--export-only", action="store_true")
    parser.add_argument("--refresh-metadata", action="store_true",
                        help="Refresh normalised metadata and summary provenance from the cached corpus.")
    parser.add_argument("--service", action="append", default=[],
                        help="Reprocess only this service URN from the existing snapshot; may be repeated.")
    args = parser.parse_args()
    for folder in ["reports", "text", "source"]:
        (ROOT / folder).mkdir(parents=True, exist_ok=True)
    connection = init_database()
    if args.refresh_metadata:
        snapshot = json.loads((ROOT / "source" / "directory.json").read_text(encoding="utf-8"))
        for source in snapshot["services"]:
            service = normalise(source)
            analysis_path = ROOT / "source" / f"{service['urn']}-analysis.json"
            pages = None
            if analysis_path.exists():
                report = json.loads(analysis_path.read_text(encoding="utf-8"))
                if report["status"] == "downloaded":
                    official = "\n\n".join(s["text"] for s in report.get("sections", [])
                                            if s["theme"] == "Summary")
                    if official and official == report.get("summary"):
                        report["summary_kind"] = "Official CIW summary"
                    elif not report.get("summary"):
                        extracted = extract_report(ROOT / report["file"], service["urn"])
                        pages = extracted.pop("_pages")
                        report.update(extracted)
                        report["analysed_at"] = datetime.now(timezone.utc).isoformat()
                    elif not report.get("summary_kind"):
                        raise ValueError(f"{service['urn']}: summary provenance cannot be established.")
                    report["findings"] = [f for f in report.get("findings", [])
                                          if f["kind"] != "Published action table"]
                    for entry in report.get("action_entries", []):
                        if not entry["none_identified"]:
                            report["findings"].append({
                                "text": entry["text"], "page": entry["page"], "topics": entry["topics"],
                                "kind": "Published action table", "action_category": entry["category"],
                                "action_status": entry["status"], "date_identified": entry["date_identified"]})
                    write_json(analysis_path, report)
                service["report"] = report
            elif service["inspection"]:
                raise RuntimeError(f"{service['urn']}: cached report analysis is missing.")
            save_service(connection, source, service, pages)
        data = export_catalogue(connection, snapshot)
        print(json.dumps(data["meta"], indent=2))
        connection.close()
        return
    if args.export_only:
        snapshot = json.loads((ROOT / "source" / "directory.json").read_text(encoding="utf-8"))
        export_catalogue(connection, snapshot)
        return
    session = make_session()
    snapshot = capture_directory(session)
    unknown_urns = set(args.service) - {s["urn"] for s in snapshot["services"]}
    if unknown_urns:
        raise ValueError("Requested service URN is not in this snapshot: " + ", ".join(sorted(unknown_urns)))
    connection.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?)", (
        SNAPSHOT_DATE, snapshot["captured_at"], FILTER, snapshot["filter_label"],
        snapshot["total"], json.dumps({k: v for k, v in snapshot.items() if k != "services"})))
    connection.commit()
    # Persist the full directory before downloading; interruption cannot lose the inventory.
    for source in snapshot["services"]:
        exists = connection.execute("SELECT 1 FROM services WHERE urn=?", (source["urn"],)).fetchone()
        if not exists:
            service = normalise(source)
            if service["inspection"]:
                service["report"]["status"] = "pending"
            save_service(connection, source, service)
    export_catalogue(connection, snapshot)
    for index, source in enumerate(snapshot["services"], 1):
        if args.service and source["urn"] not in args.service:
            continue
        service = normalise(source)
        pages = download_report(session, source, service, args.reextract)
        save_service(connection, source, service, pages)
        if index % 25 == 0 or index == snapshot["total"]:
            data = export_catalogue(connection, snapshot)
            print(f"Reports: {index}/{snapshot['total']} | {data['meta']['report_counts']}", flush=True)
        if not args.reextract:
            time.sleep(0.12)
    data = export_catalogue(connection, snapshot)
    print(json.dumps(data["meta"], indent=2), flush=True)
    connection.close()


if __name__ == "__main__":
    main()
