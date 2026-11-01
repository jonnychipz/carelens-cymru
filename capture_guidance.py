"""Archive the official rating guidance alongside, but separate from, the service reports."""
import hashlib
import json
from datetime import datetime, timezone

import fitz
import requests

from collect import ROOT, make_session, write_json

PAGE = "https://www.careinspectorate.wales/ratings-care-home-and-domiciliary-support-services"
DOCUMENTS = [
    ("inspection-framework.pdf", "Framework for inspection of accommodation based and domiciliary support services",
     "https://www.careinspectorate.wales/sites/default/files/2025-05/250508-framework-Inspecting-Adult-and-Children-en.pdf"),
    ("displaying-ratings.pdf", "A guide for care providers on how to display ratings - April 2025",
     "https://www.careinspectorate.wales/sites/default/files/2025-03/250320-a-guide-to-displaying-ratings-en.pdf"),
    ("ratings-provider-faq.pdf", "Understanding ratings: Provider FAQs",
     "https://www.careinspectorate.wales/sites/default/files/2025-03/250324-ratings-FAQ-providers-en.pdf"),
    ("ratings-pre-launch-checklist.pdf", "Getting ready for ratings: provider checklist",
     "https://www.careinspectorate.wales/sites/default/files/2025-03/250312-getting%20readyforratings-en_0.pdf"),
]


def main():
    folder = ROOT / "guidance"
    folder.mkdir(exist_ok=True)
    response = requests.get(PAGE, timeout=60)
    response.raise_for_status()
    (folder / "ratings-source-page.html").write_text(response.text, encoding="utf-8")
    session = make_session()
    response = session.get("https://digital.careinspectorate.wales/backend/directory/getreferencedata", timeout=60)
    response.raise_for_status()
    write_json(ROOT / "source" / "reference-data.json", response.json())
    documents = []
    for filename, title, url in DOCUMENTS:
        response = requests.get(url, timeout=90)
        response.raise_for_status()
        if not response.content.startswith(b"%PDF"):
            raise ValueError(f"Guidance URL did not return a PDF: {url}")
        path = folder / filename
        path.write_bytes(response.content)
        with fitz.open(path) as pdf:
            pages = len(pdf)
            text = "\n\n".join(f"--- PAGE {i+1} ---\n{p.get_text()}" for i, p in enumerate(pdf))
        path.with_suffix(".txt").write_text(text, encoding="utf-8")
        documents.append({"title": title, "source_url": url, "file": "guidance/" + filename,
                          "sha256": hashlib.sha256(response.content).hexdigest(),
                          "bytes": len(response.content), "pages": pages})
    write_json(ROOT / "guidance.json", {
        "captured_at": datetime.now(timezone.utc).isoformat(), "source_page": PAGE,
        "documents": documents,
        "rating_labels": ["Excellent", "Good", "Requires Improvement", "Requires Significant Improvement"],
        "context": "CIW states that published ratings began in April 2025. The directory is a snapshot, not a complete rating history."})
    print(json.dumps({"guidance_documents": len(documents), "reference_data": "captured"}))


if __name__ == "__main__":
    main()
