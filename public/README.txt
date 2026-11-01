CareLens Cymru
Inspection evidence. Better care.

An independent, source-linked learning and reporting application for care home
managers, based on the Care Inspectorate Wales adult care home directory.

Live site: https://jonnychipz.github.io/carelens-cymru/
Repository: https://github.com/jonnychipz/carelens-cymru
Developed by John Lunn.

PUBLIC SNAPSHOT
1 November 2026: 1,046 adult care home services across 22 local authorities.
980 latest published inspection reports, 858 rated services, 188 with no
published theme ratings, and 66 with no latest published report.
980 source-based synopses: 979 official summary extracts and one explicitly
labelled theme-based synopsis where CIW's Summary section is blank.

USE
Open the live site or public/index.html locally. Keep the public folder intact
for local PDF, report and database links. The site does not require a server,
third-party chart libraries, external fonts, tracking, or sign-in.

Explore cohort filters, published theme ratings, care-home details, and
page-linked inspection learning. Every report remains linked to its original
CIW source. Read the baseline and monthly report library or download its
actual PDF files. Report charts and source tables drill back into the homes
and evidence represented in the chart.
Each home opens in a right-hand evidence drawer with an ordinal four-theme
rating profile, source-linked key points and separate topic-level RAG triage.
Those topic signals are not official CIW ratings or verified current risks.
Overview displays all four theme donuts together; combined, self-excluding
filter facets update the available options and every chart. The geographic map
uses CIW coordinates, follows those filters and opens the same evidence drawer.
Home table headers sort ascending/descending; monthly reports are newest first.
PDF source links and manager-report links open in a separate tab by default.

REPORTS
The baseline uses all records in the snapshot. Monthly reports group the latest
archived report for each home by CIW report publication month. They are NOT all
inspections conducted in a month, a complete historic report series, or a
longitudinal measure of service quality. Current published ratings are snapshot
ratings, not historic effective-date observations. Empty months are explicit.

Manager prompts, improvement ideas and assurance workflows are suggestions,
separate from CIW's published requirements. Automated wording labels are
discovery aids and may require interpretation. Action-table rows retain source
dates/statuses; historical or Achieved entries are not current failures.

FILES
public/index.html              Self-contained application and embedded evidence.
public/catalogue.json          Contact-minimised public structured catalogue.
public/catalogue.sqlite        Public relational database and full PDF page text.
public/report-library.json     Fixed baseline/monthly report cohorts and evidence.
public/reports/                980 unchanged latest public CIW inspection PDFs.
public/text/                   Full extracted inspection text by PDF page.
public/management-reports/     Readable HTML and downloadable PDF manager reports.
public/assets/                 Original CareLens Cymru SVG branding.
public/guidance/               Official reference documents and copyright context.
public/release-manifest.json   File sizes and SHA-256 hashes.
public/validation.json         Archive completeness and extraction-review record.
PUBLIC-DATA.txt                Public scope and public database schema.
DATA-DICTIONARY.txt            Research methodology and detailed source schema.

SOURCE AND REBUILD
Python dependencies are listed in requirements.txt. The published site already
contains the approved snapshot; Python is not required just to use it.
Acquisition runs use the public website's normal anti-forgery session and do not
store tokens or credentials. Requests are paced and retry transient errors.

From a local research copy:
  python collect.py
  python collect.py --reextract
  python validate_catalogue.py
  python capture_guidance.py
  python generate_reports.py
  python build_portal.py
  python prepare_release.py
  python build_portal.py --data public/catalogue.json --output-dir public --public
  python validate_release.py

The private raw research archive is deliberately git-ignored. Downloading this
public repository does not restore private directory contacts. To build a new
snapshot, acquire source records separately in a new research folder; preserve
previous snapshots. The acquisition code reuses an existing directory.json
instead of silently replacing historical evidence.

DEPLOYMENT
The GitHub Actions Pages workflow publishes only public/ on pushes to main.
No server, API key or environment secret is required.
The map uses vendored Leaflet 1.9.4 (BSD-2-Clause licence in assets/vendor)
and OpenStreetMap standard raster tiles. Street tiles and source-hosted logos
require internet access; normal tile/image requests disclose the requested
resource to the provider, not the inspection corpus. OpenStreetMap contributors
are attributed on the map. Do not bulk-download tiles or remove attribution.

LICENCE AND INDEPENDENCE
Original code and CareLens Cymru branding: MIT licence.
CIW / Welsh Government reports and extracts: Crown copyright and Open
Government Licence where stated. See LICENSE, source documents and
https://www.careinspectorate.wales/copyright .
CareLens Cymru is not an official CIW service and implies no CIW endorsement.
