"""Generate fixed-cohort CareLens Cymru learning reports from captured CIW evidence.

No acquisition, rating inference, catalogue mutation or external resources are used.
"""
import argparse
import calendar
from collections import Counter
from datetime import date
import html
import json
from pathlib import Path
import re

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
    KeepTogether, Flowable,
)


LIMITATIONS = [
    "This is a fixed all-data period cohort, not a dashboard-filter cohort. Only each service's latest archived report is included; this is not the complete historical inspection series or a trend in care quality. Early empty periods must not be interpreted as an absence of historical inspections: earlier reports are outside this latest-only archive.",
    "Monthly membership uses reportPublishedActualDate, not the inspection visit or report issue date. Visit start/end, issue and publication are distinct. Empty months mean no latest archived report was published in that month, not that CIW did no inspections.",
    "Ratings are the exact four published directory-snapshot theme values. Their effective dates are not independently auditable and cannot be assumed to match the historical publication month. Missing ratings and missing reports are not evidence of poor care. No overall quality score is calculated.",
    "Positive wording and Improvement wording are automated search labels, not independent CIW findings. Topic counts mean reports mentioning a topic, not failures; topics and evidence sets overlap and must not be added together.",
    "Published action-table rows can be historical or Achieved. New and Reviewed describe status words published in the source, not validation today. Missing status means unknown, not unresolved. Category counts are not counts of current failures or confirmed enforcement notices.",
    "Excerpts and official summaries retain extracted wording and PDF page provenance. Layout changes and damaged characters can occur; the archived PDF is authoritative. Representative examples are not an exhaustive account or a prevalence estimate.",
    "Public latest-report URLs can change after this snapshot. Use the linked local archived PDF to verify the captured evidence; the external source link establishes its CIW origin.",
    "Management controls and the illustrative learning workflow are assistant-suggested local learning prompts, not CIW requirements, legal advice, clinical advice or an accreditation assessment. Validate applicability with appropriate local leads.",
    "CareLens Cymru is independent and not an official CIW product. Source: Care Inspectorate Wales / Welsh Government, Crown copyright in each report's stated year; reuse under the Open Government Licence, excluding logos. 979 archived reports contain explicit licence notices. The Haven has no extracted notice; the general CIW copyright page provides Crown-material reuse permission. Source links and attribution are retained.",
]
OGL = "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/"
COPYRIGHT = "https://www.careinspectorate.wales/copyright"
CONTROL_TESTS = {
    "medicines": ("Sample administration records against actual staff practice with the medicines lead.",
                  "Exceptions are detected, followed up and checked; staff demonstrate the agreed local procedure."),
    "safeguarding": ("Walk through a local escalation scenario and trace a recent incident review.",
                     "Staff explain escalation confidently and can show how incident learning changed practice."),
    "care-plans": ("Compare sampled plans with current preferences, changing needs and observed care.",
                   "Plans are usable and consistent with the support people actually receive."),
    "staffing": ("Compare recent rotas and induction checks with actual support needs and continuity.",
                 "The agreed staffing arrangements work in observed practice; identified gaps receive follow-up."),
    "training": ("Ask staff to demonstrate a role-relevant skill and compare supervision evidence.",
                 "Competence is demonstrated in practice rather than inferred from attendance records alone."),
    "governance": ("Trace an audit exception and resident feedback through ownership, action and recheck.",
                   "Oversight detects gaps and verifies improvements with records, observation and resident feedback."),
    "rights": ("Observe daily choices and compare decision records with what people describe.",
               "People's choices are respected in practice and applicable decision records remain coherent."),
    "nutrition": ("Observe a meal and compare preferences, monitoring records and escalation arrangements.",
                   "People describe a suitable dining experience and changing needs receive documented follow-up."),
    "infection": ("Observe agreed cleaning and hygiene routines alongside the local audit evidence.",
                   "Routines work in practice and audit exceptions are corrected and rechecked."),
    "environment": ("Walk the premises and trace sampled safety or maintenance exceptions to verification.",
                     "Identified hazards receive appropriate follow-up and checks reflect actual conditions."),
    "welsh": ("Ask how language preferences are identified and observe the active offer in practice.",
               "People can access the agreed language and cultural support, not merely a recorded preference."),
    "activities": ("Discuss meaningful goals with residents and compare participation records with experience.",
                    "People describe opportunities that matter to them and feedback shapes future support."),
}
NEGATIVE = re.compile(
    r"\b(?:not|cannot|unable|lack|lacked|lacking|inadequate|poor|fail\w*|"
    r"required|requires?|needed|needs?|improv\w*|inconsistent|however|although|but|"
    r"concern\w*|shortfall\w*|must|should|without|outstanding)\b", re.I
)
DIRECT_NEGATIVE = re.compile(
    r"\b(?:not|cannot|unable|inadequate|lack\w*|fail\w*|requires?\s+improvement|"
    r"need(?:s|ed)?\s+to|must|should)\b", re.I
)
BOILERPLATE = re.compile(
    r"where we (?:identify|find)|we (?:may|can) (?:issue|take)|"
    r"CIW has (?:no areas|not issued)|inspection report|date\(s\) of inspection|"
    r"service information|operated by|registered places|main language\(s\)", re.I
)
CONTACT = re.compile(r"[\w.+-]+@[\w.-]+\.\w+|(?<!\w)(?:\+44|0)\s*(?:\d[\s()-]*){9,12}(?!\w)")


def archived(service):
    report = service.get("report", {})
    return report.get("status") == "downloaded" and bool(report.get("file"))


def publication(service):
    return (service.get("inspection") or {}).get("reportPublishedActualDate", "")[:10]


def action_rows(service):
    return [a for a in service.get("report", {}).get("action_entries", [])
            if not a.get("none_identified")]


def is_priority(row):
    return row.get("category", "").casefold() == "priority action"


def safe_excerpt(finding, kind):
    text = finding.get("text", "")
    if len(text) < 45 or len(text) > 1100 or CONTACT.search(text) or BOILERPLATE.search(text):
        return False
    if kind == "positive":
        return not NEGATIVE.search(text)
    return bool(DIRECT_NEGATIVE.search(text))


def sample(candidates, limit):
    """Prefer readable complete short excerpts; keep separate service examples."""
    candidates.sort(key=lambda e: (abs(len(e["text"]) - 210), e["name"], e["page"]))
    chosen, seen = [], set()
    for candidate in candidates:
        if candidate["urn"] not in seen:
            chosen.append(candidate)
            seen.add(candidate["urn"])
        if len(chosen) == limit:
            break
    return chosen


def source_record(service):
    report = service.get("report", {})
    inspection = service.get("inspection") or {}
    return {
        "urn": service["urn"], "name": service["name"],
        "provider": service.get("provider"), "authority": service.get("local_authority"),
        "visit_start": inspection.get("firstSuccessfulSiteVisit") or inspection.get("inspectionActualDate"),
        "visit_end": inspection.get("lastSuccessfulSiteVisit") or inspection.get("inspectionCompletedActualDate"),
        "report_issue_date": inspection.get("reportIssuedActualDate"),
        "published_date": inspection.get("reportPublishedActualDate"),
        "ratings": service.get("ratings", {}),
        "summary_kind": report.get("summary_kind"),
        "summary": report.get("summary", ""),
        "summary_pages": report.get("summary_source_pages", []),
        "report_file": report.get("file"),
        "source_url": service.get("source_url"),
        "report_endpoint_url": report.get("source_url"),
        "directory_url": service.get("source_url"),
    }


def make_report(data, cohort, identifier, period_start, period_end):
    homes = [s for s in cohort if archived(s)]
    rows = [a for s in homes for a in action_rows(s)]
    all_findings = [f for s in homes for f in s["report"].get("findings", [])]
    rated = sum(bool(s.get("ratings")) for s in cohort)
    summary = {
        "services": len(cohort), "reports": len(homes), "rated_services": rated,
        "unrated_services": len(cohort) - rated,
        "authorities": len({s.get("local_authority") for s in cohort}),
        "pages": sum(s["report"].get("pages", 0) for s in homes),
        "specific_action_rows": len(rows),
        "priority_action_services": sum(any(is_priority(a) for a in action_rows(s)) for s in homes),
        "improvement_action_services": sum(any(a.get("category") == "Area for improvement"
                                              for a in action_rows(s)) for s in homes),
        "achieved_action_rows": sum(a.get("status") == "Achieved" for a in rows),
        "unknown_status_action_rows": sum(not a.get("status") for a in rows),
        "positive_evidence_services": sum(any(f.get("kind") == "Positive wording"
                                                for f in s["report"].get("findings", [])) for s in homes),
        "improvement_wording_services": sum(any(f.get("kind") == "Improvement wording"
                                                  for f in s["report"].get("findings", [])) for s in homes),
    }
    ratings = {theme: dict(Counter(s.get("ratings", {}).get(theme) or "Not published"
                                  for s in cohort)) for theme in data["themes"]}
    coverage = {
        "no_archived_report_services": len(cohort) - len(homes),
        "official_summary_reports": sum(s["report"].get("summary_kind") == "Official CIW summary" for s in homes),
        "labelled_theme_extract_reports": sum(s["report"].get("summary_kind") != "Official CIW summary" for s in homes),
        "finding_rows": len(all_findings),
        "finding_rows_by_search_label": dict(Counter(f.get("kind") for f in all_findings)),
        "action_rows_by_category": dict(Counter(a.get("category") for a in rows)),
        "action_rows_by_published_status": dict(Counter(a.get("status") or "Unknown" for a in rows)),
        "action_rows_without_topic": sum(not a.get("topics") for a in rows),
        "section_count": sum(len(s["report"].get("sections", [])) for s in homes),
        "section_theme_reports": {theme: sum(any(sec.get("theme") == theme for sec in s["report"].get("sections", []))
                                            for s in homes) for theme in data["themes"]},
        "authority_services": dict(sorted(Counter(s.get("local_authority") or "Not recorded" for s in cohort).items())),
        "report_denominator": len(homes), "rating_denominator": len(cohort),
    }
    topics, actions = [], []
    for definition in data["topics"]:
        tid = definition["id"]
        sets = {k: set() for k in ["positive", "negative", "action", "priority", "mention", "section"]}
        examples = {k: [] for k in ["positive", "negative", "actions"]}
        for service in homes:
            report = service["report"]
            base = {"urn": service["urn"], "name": service["name"]}
            for section in report.get("sections", []):
                if tid in section.get("topics", []):
                    sets["section"].add(service["urn"])
            for finding in report.get("findings", []):
                if tid not in finding.get("topics", []):
                    continue
                sets["mention"].add(service["urn"])
                kind = {"Positive wording": "positive", "Improvement wording": "negative"}.get(finding.get("kind"))
                if kind:
                    sets[kind].add(service["urn"])
                    if safe_excerpt(finding, kind):
                        examples[kind].append({**base, "text": finding["text"], "page": finding["page"]})
            for entry in action_rows(service):
                if tid not in entry.get("topics", []):
                    continue
                sets["action"].add(service["urn"])
                if is_priority(entry):
                    sets["priority"].add(service["urn"])
                if not CONTACT.search(entry["text"]):
                    examples["actions"].append({**base, **{k: entry.get(k) for k in
                                                          ["text", "page", "category", "status", "date_identified"]}})
        topic = {
            "id": tid, "name": definition["name"], "positive_services": len(sets["positive"]),
            "improvement_wording_services": len(sets["negative"]), "action_services": len(sets["action"]),
            "priority_services": len(sets["priority"]), "report_mentions": len(sets["mention"]),
            "section_mentions": len(sets["section"]), "actions": definition["action"],
            "owner": definition["owner"], "evidence": definition["evidence"],
            "examples": {k: sample(v, 5 if k == "actions" else 3) for k, v in examples.items()},
            "source_urns": sorted(sets["mention"] | sets["section"] | sets["action"]),
            "local_test": CONTROL_TESTS[tid][0], "success_outcome": CONTROL_TESTS[tid][1],
        }
        topics.append(topic)
        if topic["source_urns"]:
            actions.append({
                "topic_id": tid, "title": f"Test the local {definition['name'].lower()} control",
                "why": f"{len(sets['mention'])} of {len(homes)} archived reports mention this topic; "
                       f"{len(sets['action'])} include matching published action rows (possibly historical). "
                       "Use both positive practice and weaknesses as learning material, not a prediction about your home.",
                "suggested_action": definition["action"], "owner": definition["owner"],
                "assurance_evidence": definition["evidence"], "source_urns": topic["source_urns"],
                "local_test": topic["local_test"], "success_outcome": topic["success_outcome"],
            })
    title = ("Baseline manager learning report" if identifier == "baseline"
             else f"{date.fromisoformat(identifier + '-01'):%B %Y} publication-month manager learning report")
    narrative = [
        f"The fixed cohort contains {len(cohort):,} homes across {summary['authorities']} authorities; "
        f"{len(homes):,} latest archived reports provide {summary['pages']:,} source PDF pages.",
        f"{coverage['official_summary_reports']:,} reports have official CIW summaries and "
        f"{coverage['labelled_theme_extract_reports']} have explicitly labelled theme extracts. "
        f"{coverage['no_archived_report_services']} cohort homes have no archived report.",
        f"{rated:,} homes have at least one published theme rating and {len(cohort)-rated:,} have none. "
        "The four theme distributions retain missing values and use the full cohort denominator.",
        f"Published tables contain {len(rows)} specific rows: "
        f"{coverage['action_rows_by_category'].get('Priority action', 0)} Priority action and "
        f"{coverage['action_rows_by_category'].get('Area for improvement', 0)} Area for improvement. "
        f"{summary['achieved_action_rows']} rows are Achieved; {summary['unknown_status_action_rows']} have unknown status.",
        f"{coverage['action_rows_without_topic']} specific action rows have no topic assignment and remain "
        "included in the whole-cohort row totals; topic charts must not be treated as a complete action inventory.",
        f"All {len(all_findings):,} extracted finding rows and {coverage['section_count']:,} sections inform "
        "the evidence map. Positive and Improvement wording counts are separate overlapping search-label cohorts.",
    ] if homes else ["No latest archived report published in this month at this snapshot.",
                      "This empty fixed publication cohort supplies no inspection evidence, rating distribution or evidence-derived management priorities."]
    if len(homes) == 1:
        narrative.append("One report only: this is a sourced learning case, not a prevalence estimate or a comparison between homes.")
    if not homes:
        topics, actions = [], []
    if identifier == data["meta"]["snapshot_date"][:7] and identifier != "baseline":
        narrative.append(f'This snapshot-month cohort is captured as of {data["meta"]["snapshot_date"]}; it is not a completed-month account or a prediction of later publications.')
    return {
        "id": identifier, "kind": "baseline" if identifier == "baseline" else "monthly",
        "title": title, "month": None if identifier == "baseline" else identifier,
        "period_start": period_start, "period_end": period_end,
        "service_urns": [s["urn"] for s in cohort], "summary": summary, "narrative": narrative,
        "ratings": ratings, "topics": topics, "actions": actions, "coverage": coverage,
        "pdf": f"management-reports/{identifier}.pdf", "html": f"management-reports/{identifier}.html",
        "limitations": LIMITATIONS, "source_index": [source_record(s) for s in cohort],
    }


CSS = """
:root{color-scheme:light;--cp-bg:#f3f6f8;--cp-surface:#fff;--cp-text:#18232b;--cp-muted:#596976;--cp-accent:#b62948;--cp-line:#d7e0e6;--cp-positive:#267469;--cp-issue:#916034;--cp-soft:#eaf0f4;--cp-positive-soft:#edf6f3;--cp-issue-soft:#faf3eb;--cp-ink:var(--cp-text);--cp-primary:var(--cp-accent)}
:root[data-theme=dark]{color-scheme:dark;--cp-bg:#121b23;--cp-surface:#1c2933;--cp-text:#edf2f5;--cp-muted:#b2c2ce;--cp-accent:#ff839b;--cp-line:#40515f;--cp-positive:#86d1bf;--cp-issue:#e6bc89;--cp-soft:#283a47;--cp-positive-soft:#243c36;--cp-issue-soft:#3e342c}
*{box-sizing:border-box}body{margin:0;background:var(--cp-bg);color:var(--cp-text);font:16px/1.6 'Segoe UI',Arial,sans-serif}
main{max-width:1150px;margin:auto;padding:32px}header{padding:32px;background:var(--cp-surface);color:var(--cp-text);border:1px solid var(--cp-line);border-radius:18px}.brand{display:flex;gap:12px;align-items:center;justify-content:space-between}.brand-wordmark{display:flex;gap:12px;align-items:center;font-size:1.2rem;font-weight:600}.brand-mark{width:44px;height:44px}.brand-mark .lens{stroke:var(--cp-accent)}.brand-mark .home{stroke:var(--cp-text)}.brand-mark .focus{fill:var(--cp-accent)}
h1{font-size:2.2rem;line-height:1.2}h2{font-size:1.6rem}h3{font-size:1.2rem}a{color:var(--cp-primary)}section,.topic{background:var(--cp-surface);padding:24px;margin:22px 0;border:1px solid var(--cp-line);border-radius:14px}
.eyebrow{font-size:1rem;color:var(--cp-muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:12px}.metric{padding:16px;border:1px solid var(--cp-line);border-radius:10px}.metric b{display:block;font-size:1.4rem;color:var(--cp-text)}.muted,small{color:var(--cp-muted)}
table{border-collapse:collapse;width:100%;font-size:.9rem}th,td{text-align:left;vertical-align:top;padding:10px;border-bottom:1px solid var(--cp-line)}th{background:var(--cp-soft)}.table-wrap{overflow-x:auto}
blockquote{margin:12px 0;padding:14px 18px;border:1px solid var(--cp-line);border-radius:10px;background:var(--cp-positive-soft)}blockquote.issue{background:var(--cp-issue-soft)}blockquote p{white-space:pre-line}details{border-top:1px solid var(--cp-line);padding:14px 0}summary{cursor:pointer;font-weight:600}.source-summary{white-space:pre-line}
svg{width:100%;height:auto}input,select,button{font:inherit;border:1px solid var(--cp-line);padding:10px;border-radius:6px;background:var(--cp-surface);color:var(--cp-text)}button{cursor:pointer}.callout{background:var(--cp-soft);padding:16px;border-radius:8px}nav{display:flex;gap:18px;flex-wrap:wrap;margin:18px 0}.bar{display:block;background:var(--cp-soft);height:14px;min-width:110px;border-radius:4px}.bar i{display:block;height:100%;background:var(--cp-accent);border-radius:4px}footer{font-size:.85rem;padding:24px 0}.workflow{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.workflow a{background:var(--cp-soft);padding:10px;border-radius:8px}.drill{border:0;padding:0;background:var(--cp-surface);color:var(--cp-accent);text-align:left}.drill:hover{text-decoration:underline}#source-drill{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.drill-chip{border:1px solid var(--cp-line);background:var(--cp-soft);padding:10px 14px;border-radius:10px}[hidden]{display:none!important}:focus-visible{outline:2px solid var(--cp-accent);outline-offset:3px}
@media(max-width:600px){main{padding:14px}header,section,.topic{padding:18px}h1{font-size:1.7rem}}
@media print{:root,:root[data-theme=dark]{--cp-bg:#fff;--cp-surface:#fff;--cp-text:#18232b;--cp-muted:#596976;--cp-accent:#b62948;--cp-line:#d7e0e6;--cp-soft:#eaf0f4;--cp-positive-soft:#edf6f3;--cp-issue-soft:#faf3eb}body{background:var(--cp-bg)}main{max-width:none;padding:0}header{background:var(--cp-surface);color:var(--cp-text);border:1px solid var(--cp-line)}section,.topic{break-inside:avoid}input,select,.screen-only{display:none}details{display:block}.drill{color:var(--cp-text)}}
"""

REPORT_JS = """
const themeButton=document.getElementById("theme-toggle");
for(const anchor of document.querySelectorAll('a[href]'))if(/\\.pdf(?:[?#]|$)/i.test(anchor.getAttribute('href'))){anchor.target="_blank";anchor.rel="noopener noreferrer";}
function setTheme(mode){
 document.documentElement.dataset.theme=mode;
 themeButton.textContent=mode==="dark"?"Use light theme":"Use dark theme";
 themeButton.setAttribute("aria-pressed",String(mode==="dark"));
}
setTheme("light");
themeButton.addEventListener("click",()=>{
 const mode=document.documentElement.dataset.theme==="dark"?"light":"dark";setTheme(mode);
});
const q=document.getElementById("topic-search");
if(q)q.addEventListener("input",()=>{const t=q.value.toLowerCase();document.querySelectorAll(".topic").forEach(x=>x.hidden=!x.textContent.toLowerCase().includes(t));});
const cohort=JSON.parse(document.getElementById("cohort-data").textContent);
const records=new Map(cohort.map(x=>[x.urn,x]));
const sourceHomes=[...document.querySelectorAll("#sources details[data-urn]")];
const chip=document.getElementById("drill-label"),clear=document.getElementById("clear-drill");
function showDrill(matches,label){
 let count=0;sourceHomes.forEach(home=>{home.hidden=!matches(records.get(home.dataset.urn));if(!home.hidden)count++;});
 chip.textContent=label+" · "+count+" of "+cohort.length+" fixed-cohort homes";
 clear.hidden=false;document.getElementById("sources").scrollIntoView({block:"start"});
 document.getElementById("source-heading").focus({preventScroll:true});
}
document.addEventListener("click",event=>{
 const button=event.target.closest("button[data-drill]");if(!button)return;
 if(button.dataset.drill==="rating"){
  const theme=button.dataset.ratingTheme,rating=button.dataset.rating;
  showDrill(home=>(home.ratings[theme]||"Not published")===rating,theme+" — "+rating);
 }else if(button.dataset.drill==="authority"){
  const authority=button.dataset.authority;
  showDrill(home=>(home.authority||"Not recorded")===authority,"Authority: "+authority);
 }
});
clear.addEventListener("click",()=>{
 sourceHomes.forEach(home=>home.hidden=false);
 chip.textContent="All "+cohort.length+" fixed-cohort homes";clear.hidden=true;
});
chip.textContent="All "+cohort.length+" fixed-cohort homes";
"""


def e(value):
    return html.escape(str(value if value is not None else "Unknown"), quote=True)


def home_link(urn, label):
    return f'<a href="../index.html#home={e(urn)}">{e(label)}</a>'


def example_html(example, sources, kind):
    source = sources[example["urn"]]
    local = "../" + source["report_file"] + f'#page={example["page"]}'
    meta = ""
    if kind == "actions":
        meta = f' · {e(example["category"])} · published status: {e(example["status"])} · identified: {e(example["date_identified"])}'
    return (f'<blockquote class="{"" if kind == "positive" else "issue"}"><p>{e(example["text"])}</p>'
            f'<small>{home_link(example["urn"], example["name"])} · '
            f'<a href="{e(local)}">PDF page {e(example["page"])}</a> · '
            f'<a href="{e(source["source_url"])}">CIW source</a>{meta}</small></blockquote>')


def ratings_html(report):
    rows = []
    for theme, counts in report["ratings"].items():
        for rating, count in sorted(counts.items()):
            denom = report["summary"]["services"]
            attrs = f'data-drill="rating" data-rating-theme="{e(theme)}" data-rating="{e(rating)}"'
            cells = [e(theme), e(rating), str(count), f"{count}/{denom}",
                     f'<span class="bar"><i style="width:{count/denom*100 if denom else 0:.1f}%"></i></span>']
            rows.append("<tr>" + "".join(
                f'<td><button type="button" class="drill" {attrs} aria-label="{e(f"Show {theme}: {rating}, {count} of {denom} cohort homes")}">{cell}</button></td>'
                for cell in cells) + "</tr>")
    return '<div class="table-wrap"><table><thead><tr><th>Published theme</th><th>Rating</th><th>Homes</th><th>Denominator</th><th>Share</th></tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>'


def topic_chart(report):
    topics = report["topics"]
    denom = report["summary"]["reports"]
    height = 45 + 36 * len(topics)
    body = [f'<svg viewBox="0 0 960 {height}" role="img" aria-label="Reports mentioning each topic; counts are not failures"><title>Topic report mentions, denominator {denom}</title>']
    for i, topic in enumerate(topics):
        y = 25 + i * 36
        width = topic["report_mentions"] / max(denom, 1) * 520
        destination = "topic-" + topic["id"] if topic["source_urns"] else "sources"
        body.append(f'<a href="#{e(destination)}"><text x="0" y="{y+17}" font-size="15" fill="var(--cp-text)">{e(topic["name"])}</text><rect x="340" y="{y}" width="{width:.2f}" height="23" rx="4" fill="var(--cp-accent)"/><text x="875" y="{y+17}" font-size="15" fill="var(--cp-text)">{topic["report_mentions"]}/{denom}</text></a>')
    return "".join(body) + "</svg>"


def write_html(report, path, snapshot):
    s = report["summary"]
    sources = {record["urn"]: record for record in report["source_index"]}
    mark_path = Path(__file__).parent / "assets" / "carelens-mark.svg"
    mark = ""
    if mark_path.is_file():
        mark = re.sub(r"<style>.*?</style>", "", mark_path.read_text(encoding="utf-8"), flags=re.S)
        mark = mark.replace("<svg ", '<svg class="brand-mark" ', 1)
    compact_sources = [{"urn": source["urn"], "authority": source["authority"], "ratings": source["ratings"]}
                       for source in report["source_index"]]
    source_data = json.dumps(compact_sources, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    available = {t["id"] for t in report["topics"] if t["source_urns"]}
    workflow = '<div class="workflow">' + " → ".join(
        f'<a href="#{("topic-" + tid) if tid in available else "sources"}">{label}</a>'
        for label, tid in [("Resident experience", "activities"), ("Personal planning", "care-plans"),
                           ("Staff practice", "training"), ("Assurance", "governance"),
                           ("Resident feedback", "activities")]) + "</div>"
    parts = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="data:,">',
             f'<title>CareLens Cymru · {e(report["title"])}</title><style>{CSS}</style></head><body><main>',
             f'<header><div class="brand"><div class="brand-wordmark">{mark}<span>CareLens Cymru</span></div><button type="button" id="theme-toggle" class="screen-only" aria-pressed="false">Use dark theme</button></div><p class="eyebrow">Independent manager learning</p><h1>{e(report["title"])}</h1>',
             f'<p>Report publication month basis · {e(report["period_start"])} to {e(report["period_end"])} · snapshot {e(snapshot)}</p>',
             f'<p>Fixed all-data cohort · latest archived reports only · no overall quality score</p><a download href="{e(path.stem)}.pdf">Download actual PDF report</a> · <a href="../index.html">Open care-home catalogue</a></header>',
             '<nav><a href="#overview">Overview</a><a href="#ratings">Ratings & coverage</a><a href="#learning">Learning controls</a><a href="#sources">Full source register</a><a href="#method">Method & limitations</a></nav>',
             '<section id="overview"><h2>Read the cohort before the findings</h2><div class="grid">']
    for label, key in [("Homes", "services"), ("Archived reports", "reports"), ("Rated homes", "rated_services"),
                       ("No ratings", "unrated_services"), ("Source PDF pages", "pages"), ("Specific table rows", "specific_action_rows")]:
        parts.append(f'<div class="metric"><b>{s[key]:,}</b>{label}</div>')
    parts.append('</div>' + "".join(f'<p>{e(text)}</p>' for text in report["narrative"]) + "</section>")
    if s["reports"]:
        parts.extend(['<section id="ratings"><h2>Published theme ratings and coverage</h2><p>Each row uses all cohort homes, including Not published. Directory-snapshot ratings are not guaranteed to be effective at the historical report month. Click any rating row category, count or bar to drill into this report’s source register.</p>',
                      ratings_html(report), '<h3>Source coverage</h3><p>' +
                      e(f'{report["coverage"]["official_summary_reports"]} official summaries; {report["coverage"]["labelled_theme_extract_reports"]} labelled theme extracts; {report["coverage"]["no_archived_report_services"]} homes without archived reports.') + '</p>',
                      '<h3>Published action-table status, not current failures</h3><p>' +
                      e("; ".join(f'{k}: {v} rows' for k, v in report["coverage"]["action_rows_by_published_status"].items()) or "No specific action rows.") + '</p>',
                      '<h3>Authority cohort distribution</h3><div class="table-wrap"><table><tr><th>Authority</th><th>Homes</th></tr>' +
                      "".join(f'<tr><td><button type="button" class="drill" data-drill="authority" data-authority="{e(k)}">{e(k)}</button></td><td><button type="button" class="drill" data-drill="authority" data-authority="{e(k)}" aria-label="{e(f"Show {k}: {v} cohort homes")}">{v}</button></td></tr>' for k, v in report["coverage"]["authority_services"].items()) + '</table></div></section>',
                      '<section><h2>Evidence map across the whole report cohort</h2><p>All findings and theme sections are considered. Bars count distinct reports with topic mentions, not failures. Click a bar to read the topic evidence.</p>',
                      topic_chart(report), '</section>',
                      '<section id="learning"><h2>Turn evidence into better local care</h2><p>Illustrative assistant learning workflow, not a CIW assessment framework. Follow a step to the relevant local control and evidence.</p>' + workflow,
                      '<h3>Week 1: understand and test</h3><p>Read the relevant source pages with the named local lead. Ask residents what matters; sample current records and observe actual practice. Separate achieved or historical actions from present local gaps. Adapt positive practice only where suitable.</p>',
                      '<h3>By day 30: trial and verify</h3><p>Trial a small, agreed control change. Define the record, observation and resident-feedback evidence that would show success. Assign an owner and review date; track exceptions and unintended consequences.</p>',
                      '<h3>By day 90: sustain and listen</h3><p>Repeat the same checks, seek resident and staff feedback, and verify actions remain effective. Keep what improves experience, revise what does not and bring persistent gaps to local oversight.</p>',
                      '<label class="screen-only">Find a learning topic <input id="topic-search" type="search" placeholder="Topic or control wording"></label></section>'])
        for topic in report["topics"]:
            if not topic["source_urns"]:
                continue
            parts.extend([f'<article class="topic" id="topic-{e(topic["id"])}"><h2>{e(topic["name"])}</h2>',
                          f'<p class="muted">Distinct reports / {s["reports"]}: topic mentions {topic["report_mentions"]}; Positive wording {topic["positive_services"]}; Improvement wording {topic["improvement_wording_services"]}; matching specific action rows {topic["action_services"]}; matching Priority action category {topic["priority_services"]}. These sets overlap.</p>',
                          f'<h3>What to learn and test locally</h3><p>{e(topic["actions"])}</p>',
                          f'<p><strong>Local test:</strong> {e(topic["local_test"])}<br><strong>Outcome to verify:</strong> {e(topic["success_outcome"])}</p>',
                          f'<p><strong>Suggested owner:</strong> {e(topic["owner"])}<br><strong>Evidence of success:</strong> {e(topic["evidence"])}; triangulate with observed practice and residents’ experience.</p>'])
            for kind, heading in [("positive", "Positive practice to adapt — selected wording"), ("negative", "Weakness prevention — selected improvement wording"), ("actions", "Published action rows — check status and date")]:
                parts.append(f'<h3>{heading}</h3>')
                parts.extend(example_html(ex, sources, kind) for ex in topic["examples"][kind])
                if not topic["examples"][kind]:
                    parts.append('<p class="muted">No representative excerpt meeting the conservative selection rules. This does not establish absence of relevant evidence; consult the full source.</p>')
            parts.append("</article>")
    parts.append('<section id="sources"><h2 id="source-heading" tabindex="-1">Full source register and summaries</h2><p>All cohort homes are listed. Every archived report summary is retained here and in report-library.json; expand a home to read its exact extracted summary. Links resolve to the archived evidence or public CIW source. Drills match only this fixed report cohort, never the global dashboard.</p><div id="source-drill" class="screen-only"><output id="drill-label" class="drill-chip" aria-live="polite"></output><button type="button" id="clear-drill" hidden>Clear drill — show all cohort homes</button></div>')
    for source in report["source_index"]:
        parts.append(f'<details data-urn="{e(source["urn"])}"><summary>{e(source["name"])} · {e(source["urn"])} · published {e((source["published_date"] or "")[:10] or "No report")}</summary><p>{home_link(source["urn"], "Open home in catalogue")} · {e(source["provider"])} · {e(source["authority"])}</p>')
        parts.append(f'<p>Visit start: {e(source["visit_start"])}<br>Visit end: {e(source["visit_end"])}<br>Report issue: {e(source["report_issue_date"])}<br>Report publication: {e(source["published_date"])}</p>')
        if source["report_file"]:
            parts.append(f'<p><a href="{e("../" + source["report_file"])}">Archived inspection PDF</a> · <a href="{e(source["source_url"])}">Original CIW source</a> · summary PDF pages {e(", ".join(map(str, source["summary_pages"])))}</p><p><strong>{e(source["summary_kind"])}</strong></p><div class="source-summary">{e(source["summary"])}</div>')
        else:
            parts.append(f'<p>No latest archived report. <a href="{e(source["source_url"])}">Public CIW record</a></p>')
        parts.append("</details>")
    parts.extend(['</section><section id="method"><h2>Method, attribution and limitations</h2><ol>',
                  "".join(f"<li>{e(text)}</li>" for text in LIMITATIONS),
                  f'</ol><p><a href="{OGL}">Open Government Licence v3</a> · <a href="{COPYRIGHT}">CIW Crown-material copyright and reuse permission</a>. Counts are deterministic from catalogue.json findings, sections, action_entries and exact published ratings; no PDF ratings are regenerated.</p></section>',
                  '<footer>CareLens Cymru · source CIW / Welsh Government · Crown copyright / OGL · no directory contact details included.</footer>',
                  f'</main><script type="application/json" id="cohort-data">{source_data}</script><script>{REPORT_JS}</script></body></html>'])
    path.write_text("".join(parts), encoding="utf-8")


class LearningFlow(Flowable):
    def __init__(self):
        super().__init__()
        self.width, self.height = 174 * mm, 29 * mm

    def draw(self):
        labels = ["Resident\nexperience", "Personal\nplanning", "Staff\npractice", "Assurance", "Resident\nfeedback"]
        c = self.canv
        box = 30 * mm
        for i, label in enumerate(labels):
            x = i * 35 * mm
            c.setFillColor(colors.HexColor("#e9f3f5"))
            c.roundRect(x, 8 * mm, box, 17 * mm, 3 * mm, fill=1, stroke=0)
            c.setFillColor(colors.HexColor("#172f42"))
            c.setFont(FONT, 8)
            for j, line in enumerate(label.split("\n")):
                c.drawCentredString(x + box / 2, (18 - 4 * j) * mm, line)
            if i < 4:
                c.drawString(x + box + 1 * mm, 15 * mm, ">")
        c.setFont(FONT, 7)
        c.drawString(0, 2 * mm, "Illustrative assistant learning workflow; not an official CIW framework.")


class MentionChart(Flowable):
    def __init__(self, topics, denominator):
        super().__init__()
        self.topics, self.denominator = topics, denominator
        self.width, self.height = 174 * mm, (9 + 6 * len(topics)) * mm

    def draw(self):
        c = self.canv
        for index, topic in enumerate(self.topics):
            y = self.height - (index + 1) * 6 * mm
            c.setFont(FONT, 7.5)
            c.setFillColor(colors.HexColor("#172f42"))
            c.drawString(0, y, topic["name"])
            c.setFillColor(colors.HexColor("#e9f3f5"))
            c.rect(70*mm, y - mm, 81*mm, 3.5*mm, stroke=0, fill=1)
            c.setFillColor(colors.HexColor("#126779"))
            c.rect(70*mm, y - mm, topic["report_mentions"]/max(self.denominator, 1)*81*mm,
                   3.5*mm, stroke=0, fill=1)
            c.drawRightString(172*mm, y, f'{topic["report_mentions"]}/{self.denominator}')

PDF_INK = colors.HexColor("#18232b")
PDF_ACCENT = colors.HexColor("#b62948")
PDF_MUTED = colors.HexColor("#566571")
PDF_LINE = colors.HexColor("#dce2e7")
PDF_RATINGS = {"Excellent": colors.HexColor("#387460"), "Good": colors.HexColor("#63917f"),
               "Requires Improvement": colors.HexColor("#a56a1d"),
               "Requires Significant Improvement": PDF_ACCENT,
               "Not published": colors.HexColor("#a7b3bd")}


def draw_brand_mark(canvas, x, y, size):
    canvas.saveState()
    canvas.translate(x, y)
    canvas.scale(size / 64, size / 64)
    canvas.setStrokeColor(PDF_ACCENT)
    canvas.setLineWidth(4)
    canvas.arc(5, 7, 55, 57, 35, 290)
    canvas.setStrokeColor(PDF_INK)
    canvas.setLineWidth(2.7)
    path = canvas.beginPath()
    path.moveTo(20, 32); path.lineTo(32, 43); path.lineTo(44, 32)
    path.moveTo(23, 34); path.lineTo(23, 19); path.lineTo(41, 19); path.lineTo(41, 34)
    path.moveTo(29, 19); path.lineTo(29, 29); path.lineTo(35, 29); path.lineTo(35, 19)
    canvas.drawPath(path)
    canvas.setFillColor(PDF_ACCENT)
    canvas.circle(53, 32, 3.5, stroke=0, fill=1)
    canvas.restoreState()


class BrandHeading(Flowable):
    def __init__(self):
        super().__init__()
        self.width, self.height = 174 * mm, 25 * mm

    def draw(self):
        draw_brand_mark(self.canv, 0, 2 * mm, 21 * mm)
        self.canv.setFillColor(PDF_INK)
        self.canv.setFont(BOLD, 24)
        self.canv.drawString(26 * mm, 13 * mm, "CareLens Cymru")
        self.canv.setFillColor(PDF_MUTED)
        self.canv.setFont(FONT, 10)
        self.canv.drawString(26 * mm, 6 * mm, "Inspection evidence. Better care.")


class RatingDonuts(Flowable):
    def __init__(self, ratings, denominator):
        super().__init__()
        self.ratings, self.denominator = ratings, denominator
        self.width, self.height = 174 * mm, 86 * mm

    def draw(self):
        c = self.canv
        order = list(PDF_RATINGS)
        names = ["Well-being", "Care & support", "Environment", "Leadership"]
        for index, (theme, counts) in enumerate(self.ratings.items()):
            x = index * 43.5 * mm
            c.setFillColor(PDF_INK); c.setFont(BOLD, 9)
            c.drawCentredString(x + 21 * mm, 80 * mm, names[index])
            if index == 3:
                c.setFont(FONT, 8); c.drawCentredString(x + 21 * mm, 75 * mm, "& management")
            start = 90
            for rating in order:
                number = counts.get(rating, 0)
                if not number:
                    continue
                sweep = number / max(self.denominator, 1) * 360
                c.setFillColor(PDF_RATINGS[rating])
                c.wedge(x + 7 * mm, 43 * mm, x + 35 * mm, 71 * mm,
                        start, -sweep, stroke=0, fill=1)
                start -= sweep
            c.setFillColor(colors.white)
            c.circle(x + 21 * mm, 57 * mm, 9 * mm, stroke=0, fill=1)
            c.setFillColor(PDF_INK); c.setFont(BOLD, 12)
            c.drawCentredString(x + 21 * mm, 57 * mm, f"{self.denominator:,}")
            c.setFont(FONT, 7); c.drawCentredString(x + 21 * mm, 52 * mm, "homes")
            y = 36 * mm
            for rating in order:
                number = counts.get(rating, 0)
                if not number:
                    continue
                c.setFillColor(PDF_RATINGS[rating]); c.circle(x + 2 * mm, y + 1, 2, stroke=0, fill=1)
                c.setFillColor(PDF_INK); c.setFont(FONT, 6.6)
                label = {"Requires Improvement": "Requires improv.",
                         "Requires Significant Improvement": "Significant improv."}.get(rating, rating)
                c.drawString(x + 5 * mm, y, label)
                c.drawRightString(x + 41 * mm, y, f"{number} ({100*number/max(self.denominator,1):.0f}%)")
                y -= 5.3 * mm


class ActionStatusChart(Flowable):
    def __init__(self, counts):
        super().__init__()
        self.counts = counts
        self.width, self.height = 174 * mm, max(20, 10 + len(counts) * 10) * mm

    def draw(self):
        c = self.canv
        maximum = max([1, *self.counts.values()])
        for index, (status, number) in enumerate(self.counts.items()):
            y = self.height - (index + 1) * 10 * mm
            c.setFillColor(PDF_INK); c.setFont(FONT, 9)
            c.drawString(0, y + mm, status)
            c.setFillColor(colors.HexColor("#f1f4f6")); c.roundRect(45 * mm, y, 108 * mm, 4 * mm, 2, stroke=0, fill=1)
            c.setFillColor(PDF_RATINGS["Excellent"] if status == "Achieved" else PDF_ACCENT)
            c.roundRect(45 * mm, y, max(1, number / maximum * 108 * mm), 4 * mm, 2, stroke=0, fill=1)
            c.setFillColor(PDF_INK); c.drawRightString(171 * mm, y + mm, str(number))


FONT = "Helvetica"
BOLD = "Helvetica-Bold"


def configure_fonts():
    global FONT, BOLD
    for regular, bold in [
        (Path(r"C:\Windows\Fonts\arial.ttf"), Path(r"C:\Windows\Fonts\arialbd.ttf")),
        (Path(r"C:\Windows\Fonts\DejaVuSans.ttf"), Path(r"C:\Windows\Fonts\DejaVuSans-Bold.ttf")),
    ]:
        if regular.exists() and bold.exists():
            pdfmetrics.registerFont(TTFont("CareLens", str(regular)))
            pdfmetrics.registerFont(TTFont("CareLensBold", str(bold)))
            FONT, BOLD = "CareLens", "CareLensBold"
            break


def pdf_text(text):
    value = str(text if text is not None else "Unknown")
    if FONT == "Helvetica":
        value = value.translate(str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", "→": ">", "\ufffd": "?"}))
    # Long source strings wrap normally; control characters must not reach XML.
    return html.escape(re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value))


def write_pdf(report, path, snapshot):
    styles = getSampleStyleSheet()
    for name in ["Normal", "BodyText"]:
        styles[name].fontName = FONT
        styles[name].fontSize = 10.5
        styles[name].leading = 15.5
        styles[name].spaceAfter = 9
    for name in ["Title", "Heading1", "Heading2", "Heading3"]:
        styles[name].fontName = BOLD
        styles[name].textColor = PDF_INK
    styles["Title"].fontSize, styles["Title"].leading = 25, 30
    styles["Heading1"].fontSize, styles["Heading1"].leading = 20, 25
    styles["Heading1"].spaceBefore, styles["Heading1"].spaceAfter = 12, 12
    styles["Heading2"].fontSize, styles["Heading2"].leading = 14, 19
    styles["Heading2"].spaceBefore, styles["Heading2"].spaceAfter = 10, 8
    styles["Heading3"].fontSize, styles["Heading3"].leading = 11, 15
    styles.add(ParagraphStyle("Small", fontName=FONT, fontSize=8.5, leading=12, spaceAfter=7))
    styles.add(ParagraphStyle("Quote", parent=styles["BodyText"], fontSize=9.3, leading=13.5,
                              leftIndent=8, borderColor=colors.HexColor("#d6e1e9"),
                              borderWidth=.5, borderPadding=7, spaceBefore=5, spaceAfter=8))
    styles.add(ParagraphStyle("Cell", parent=styles["Small"], fontSize=8, leading=11, spaceAfter=0))
    story = []
    sources = {source["urn"]: source for source in report["source_index"]}

    def p(text, style="BodyText"):
        return Paragraph(pdf_text(text).replace("\n", "<br/>"), styles[style])

    def add(text, style="BodyText"):
        story.append(p(text, style))

    def table(headers, rows, widths):
        cells = [[p(v, "Cell") for v in headers]] + [[p(v, "Cell") for v in row] for row in rows]
        item = Table(cells, colWidths=widths, repeatRows=1, hAlign="LEFT")
        item.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e9f3f5")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("LINEBELOW", (0, 0), (-1, -1), .3, colors.HexColor("#d6e1e9")),
        ]))
        story.append(item)
        story.append(Spacer(1, 5 * mm))

    def link(text, url, style="Small"):
        return Paragraph(f'<link href="{html.escape(url, quote=True)}" color="#126779">{pdf_text(text)}</link>', styles[style])

    def quote(ex, kind):
        source = sources[ex["urn"]]
        caption = f'{ex["name"]} | PDF page {ex["page"]}'
        if kind == "actions":
            caption += f' | {ex["category"]} | published status: {ex["status"] or "Unknown"} | identified: {ex["date_identified"] or "Unknown"}'
        local = html.escape("../" + source["report_file"] + f'#page={ex["page"]}', quote=True)
        online = html.escape(source["source_url"], quote=True)
        provenance = Paragraph(
            pdf_text(caption) + f' · <link href="{local}" color="#126779">Archived page</link>'
            f' · <link href="{online}" color="#126779">CIW source</link>', styles["Small"])
        return KeepTogether([p(ex["text"], "Quote"), provenance])

    baseline = report["kind"] == "baseline"
    count = report["summary"]["reports"]
    story.append(BrandHeading())
    story.append(Spacer(1, 8 * mm))
    add(report["title"], "Heading1")
    add("Independent manager learning | source: CIW / Welsh Government", "Heading2")
    add(f'Publication period: {report["period_start"]} to {report["period_end"]}\nSnapshot: {snapshot}\nFixed all-data period cohort; latest archived reports only.')
    for text in report["narrative"]:
        add(text)
    add("How to use this report", "Heading2")
    add("Read the cohort and limitations first. Use the topic evidence to recognise positive practice and prevent weaknesses; test suitability in your own home. Do not treat search labels or historical action rows as present-day judgements.")
    story.append(link("Open the complete companion HTML source register and all official summaries", path.stem + ".html"))
    if not count:
        story.append(PageBreak())
        add("Empty publication cohort: interpretation", "Heading1")
        for text in LIMITATIONS:
            add(text, "Small")
    else:
        story.append(PageBreak())
        add("Coverage and exact published ratings", "Heading1")
        add("The rating denominator is all homes in this fixed cohort. Not published is explicit. Snapshot rating dates are not guaranteed to align with report publication dates.")
        story.append(RatingDonuts(report["ratings"], report["summary"]["services"]))
        rating_rows = [[theme, rating, str(number), f'{number}/{report["summary"]["services"]}']
                       for theme, counts in report["ratings"].items() for rating, number in sorted(counts.items())]
        table(["Theme", "Published rating", "Homes", "Denominator"], rating_rows, [65*mm, 48*mm, 22*mm, 35*mm])
        add("Action-table status", "Heading2")
        if report["coverage"]["action_rows_by_published_status"]:
            story.append(ActionStatusChart(report["coverage"]["action_rows_by_published_status"]))
            add("Source status, not present-day failures. Unknown means no status was captured; Achieved remains historical.", "Small")
        table(["Published status", "Specific rows"], list(report["coverage"]["action_rows_by_published_status"].items()) or [["No rows", 0]], [120*mm, 50*mm])
        add(f'{report["coverage"]["official_summary_reports"]} official summaries; {report["coverage"]["labelled_theme_extract_reports"]} labelled theme extracts. {report["coverage"]["finding_rows"]:,} finding rows and {report["coverage"]["section_count"]:,} sections were considered.')
        if baseline:
            story.append(PageBreak())
            add("Geographic and source coverage", "Heading1")
            table(["Authority", "Cohort homes"], list(report["coverage"]["authority_services"].items()), [120*mm, 50*mm])
            add(f'The baseline includes all {report["summary"]["services"]:,} homes, including {report["coverage"]["no_archived_report_services"]} without a latest archived report. All {count:,} report summaries are available in the companion HTML and JSON source register; any theme-extract fallback is explicitly labelled, not presented as an official summary.')
        story.append(PageBreak())
        add("Evidence map: mentions, not deficiencies", "Heading1")
        add(f'All counts are distinct report-bearing homes / {count}. Positive and Improvement wording are automated search-label sets. Action columns count homes with matching non-none published rows; historical and achieved rows remain included. Sets overlap.')
        table(["Topic", "Mention", "Positive", "Improvement wording", "Action", "Priority"],
              [[t["name"], t["report_mentions"], t["positive_services"], t["improvement_wording_services"],
                t["action_services"], t["priority_services"]] for t in report["topics"]],
              [63*mm, 20*mm, 20*mm, 30*mm, 19*mm, 18*mm])
        add("Distinct reports mentioning each topic", "Heading2")
        story.append(MentionChart(report["topics"], count))
        add("Interpret the patterns", "Heading2")
        ranked = sorted(report["topics"], key=lambda t: (-t["action_services"], -t["improvement_wording_services"], t["id"]))
        active = [t for t in ranked if t["source_urns"]]
        for t in active[:3]:
            add(f'{t["name"]}: {t["action_services"]} reports have matching table rows; {t["improvement_wording_services"]} have Improvement wording and {t["positive_services"]} have Positive wording. Use the source context and local testing, not a league table or an assumption of widespread failure.')
        story.append(PageBreak())
        add("A practical implementation sequence", "Heading1")
        story.append(LearningFlow())
        for heading, text in [
            ("Week 1 — understand and test", "Ask residents what matters. Read representative positive and issue evidence with the relevant lead. Sample current records and observe practice. Identify applicable local gaps, separating historical/achieved source rows from current local concerns."),
            ("By day 30 — trial and verify", "Trial a small agreed change with a named owner and review date. Define success using the listed assurance records, direct observation and resident feedback. Record exceptions and unintended consequences; adapt good practice rather than simply copy it."),
            ("By day 90 — sustain and listen", "Repeat the same checks to verify the control remains effective. Ask residents and staff whether experience improved. Retain effective changes, revise ineffective ones and bring persistent local gaps to oversight."),
        ]:
            add(heading, "Heading2")
            add(text)
        add("Success is not a completed checklist alone. Triangulate records, observed staff practice and people's experience. These are assistant-suggested learning prompts, not new legal duties or clinical instructions.")
        if count == 1:
            add("Single-report learning case", "Heading2")
            source = next(v for v in sources.values() if v["report_file"])
            add(source["name"], "Heading3")
            add(source["summary_kind"], "Small")
            add(source["summary"])
            story.append(link("Read the original CIW report", source["source_url"]))
        # Baseline gives each control its own evidence page. Monthly reports group
        # controls, with tighter example limits for small cohorts.
        pdf_topics = active[:3] if count == 1 else active
        if count == 1:
            add("Three evidence-linked controls to start with", "Heading2")
            add("These are practical starting points from this one source, not a ranked assessment. All twelve topic controls and the complete evidence map remain available in the companion HTML.", "Small")
        for index, t in enumerate(pdf_topics):
            if baseline or (count > 1 and index % 2 == 0):
                story.append(PageBreak())
            add(t["name"], "Heading1" if baseline else "Heading2")
            add(f'Reports / {count}: mentions {t["report_mentions"]}; Positive wording {t["positive_services"]}; Improvement wording {t["improvement_wording_services"]}; matching action rows {t["action_services"]}; Priority category {t["priority_services"]}. Overlapping search-label sets.', "Small")
            if baseline:
                add("What to learn and how to test locally", "Heading3")
            add(t["actions"])
            add(f'Local test: {t["local_test"]}\nOutcome to verify: {t["success_outcome"]}', "Small")
            add(f'Suggested owner: {t["owner"]}\nEvidence of success: {t["evidence"]}; cross-check against observed practice and resident feedback.', "Small")
            limit = 2 if baseline else 1
            kinds = [("positive", "Positive practice to adapt"),
                     ("negative", "Weakness prevention: direct improvement wording"),
                     ("actions", "Published table row: verify status and identified date")]
            if not baseline:
                # Avoid printing both a table row and a negative paraphrase from
                # the same small evidence base; the full HTML retains both.
                kinds = [kinds[0], kinds[2] if t["examples"]["actions"] else kinds[1]]
            for kind, heading in kinds:
                selected = t["examples"][kind][:limit]
                if selected:
                    add(heading, "Heading3")
                    for ex in selected:
                        story.append(quote(ex, kind))
            if not any(t["examples"].values()):
                add("No representative excerpt passes the conservative wording rules. Consult the full source register; absence of an example is not absence of evidence.", "Small")
        if count != 1:
            story.append(PageBreak())
        add("Source register: navigation, not a data dump", "Heading1")
        add(f'All {len(report["source_index"]):,} cohort records, including all {count:,} archived report summaries, are retained in the companion standalone HTML and report-library.json. The HTML register gives publication, issue and visit dates, exact summary kind, summary pages and individual source links. This PDF retains representative source-page links rather than a hundreds-page summary appendix.')
        story.append(link("Open full source register and every summary in the companion HTML", path.stem + ".html"))
        selected_urns = sorted({ex["urn"] for t in report["topics"] for kind in t["examples"] for ex in t["examples"][kind]})
        if not baseline:
            selected_urns = [s["urn"] for s in report["source_index"] if s["report_file"]]
        # A compact representative register complements, rather than duplicates,
        # the complete HTML/JSON register.
        for urn in selected_urns[:30 if baseline else 12]:
            source = sources[urn]
            add(f'{source["name"]} | {urn} | publication {(source["published_date"] or "")[:10]}', "Small")
            story.append(link("CIW public source", source["source_url"]))
            story.append(link("Archived report", "../" + source["report_file"]))
        story.append(PageBreak())
        add("Method, attribution and limitations", "Heading1")
        for text in LIMITATIONS:
            add(text, "Small")
        add("Method: deterministic aggregation of the full JSON findings, theme sections and specific action rows. Topic examples are conservatively selected exact extracted wording, preferring readable short passages from distinct homes. Positive examples exclude negative/qualification vocabulary; issue examples require direct negative or required-improvement wording. Generic enforcement boilerplate and contact-bearing/header excerpts are excluded. These selection rules change displayed examples, not the original search-label counts.", "Small")
        story.append(link("Open Government Licence v3", OGL))
        story.append(link("CIW Crown-material copyright and reuse permission", COPYRIGHT))

    def page_decoration(canvas, doc):
        canvas.saveState()
        canvas.setTitle(f'CareLens Cymru | {report["title"]}')
        canvas.setAuthor("CareLens Cymru | CIW public-source learning")
        canvas.setSubject("Fixed cohort manager learning; report publication month basis")
        draw_brand_mark(canvas, 18 * mm, 282.5 * mm, 10 * mm)
        canvas.setFont(BOLD, 9)
        canvas.setFillColor(PDF_INK)
        canvas.drawString(31*mm, 289*mm, "CareLens Cymru")
        canvas.setFont(FONT, 7.5); canvas.setFillColor(PDF_MUTED)
        canvas.drawString(31*mm, 284*mm, report["id"] + "  |  latest archived reports only")
        canvas.setStrokeColor(PDF_LINE); canvas.line(18*mm, 281*mm, 192*mm, 281*mm)
        canvas.drawString(18*mm, 12*mm, "CIW / Welsh Government | Crown copyright / Open Government Licence")
        canvas.drawRightString(192*mm, 12*mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(str(path), pagesize=(210*mm, 297*mm),
                            rightMargin=18*mm, leftMargin=18*mm, topMargin=23*mm,
                            bottomMargin=21*mm, title=f'CareLens Cymru | {report["title"]}')
    doc.build(story, onFirstPage=page_decoration, onLaterPages=page_decoration)


def month_range(first, last):
    year, month = map(int, first.split("-"))
    while (key := f"{year:04}-{month:02}") <= last:
        yield key
        month += 1
        if month == 13:
            year, month = year + 1, 1


def validate(library, output, data):
    import fitz
    reports = library["reports"]
    baseline = reports[0]
    months = reports[1:]
    assert sum(r["summary"]["reports"] for r in months) == baseline["summary"]["reports"]
    assert len({urn for r in months for urn in r["service_urns"]}) == baseline["summary"]["reports"]
    expected_urns = Counter(s["urn"] for s in data["services"] if archived(s))
    monthly_urns = Counter(urn for r in months for urn in r["service_urns"])
    assert monthly_urns == expected_urns and all(count == 1 for count in monthly_urns.values())
    assert [r["month"] for r in months] == list(month_range(
        library["meta"]["earliest_publication_month"], library["meta"]["latest_report_month"]))
    page_counts = {}
    originals = {s["urn"]: s for s in data["services"]}
    exact_examples = 0
    for report in reports:
        assert len(report["service_urns"]) == report["summary"]["services"]
        assert len(report["source_index"]) == report["summary"]["services"]
        for counts in report["ratings"].values():
            assert sum(counts.values()) == report["summary"]["services"]
        if report["kind"] == "monthly":
            assert all(s["published_date"][:7] == report["month"] for s in report["source_index"])
        for source in report["source_index"]:
            assert source["summary"] == originals[source["urn"]]["report"].get("summary", "")
        for topic in report["topics"]:
            for kind, examples in topic["examples"].items():
                for example in examples:
                    original = originals[example["urn"]]["report"]
                    records = original["action_entries"] if kind == "actions" else original["findings"]
                    assert any(row["text"] == example["text"] and row["page"] == example["page"] for row in records)
                    exact_examples += 1
        with fitz.open(output / report["pdf"]) as pdf:
            assert len(pdf) > 0 and all(page.get_text().strip() for page in pdf)
            assert all("CareLens Cymru" in page.get_text() for page in pdf)
            page_counts[report["id"]] = len(pdf)
            report["coverage"]["generated_pdf_pages"] = len(pdf)
            assert pdf.metadata["title"].startswith("CareLens Cymru")
            for page in pdf:
                for block in page.get_text("blocks"):
                    assert block[0] >= 0 and block[1] >= 0 and block[2] <= page.rect.width + 1 and block[3] <= page.rect.height + 1
        assert (output / report["html"]).is_file()
    # The emitted model is allowlisted, never a serialised directory record.
    banned = {"email", "phone", "responsible_individuals", "raw_json", "conditions", "imposed_conditions"}
    def check(value):
        if isinstance(value, dict):
            assert not banned.intersection(value)
            for child in value.values():
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)
    check(library)
    library["meta"]["validation"] = {
        "all_pdfs_readable": True, "all_pdf_pages_nonempty": True,
        "pdf_text_within_page_bounds": True, "monthly_report_total_matches_baseline": True,
        "every_archived_urn_exactly_once_across_months": True,
        "publication_month_range_contiguous_through_snapshot": True,
        "directory_contact_fields_excluded": True, "pdf_count": len(reports),
        "all_summaries_match_source": True, "exact_source_page_examples_verified": exact_examples,
        "baseline_pdf_pages": page_counts["baseline"],
        "monthly_pdf_pages_min": min(page_counts[r["id"]] for r in months),
        "monthly_pdf_pages_max": max(page_counts[r["id"]] for r in months),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path(__file__).parent / "catalogue.json")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent)
    args = parser.parse_args()
    data = json.loads(args.data.read_text(encoding="utf-8"))
    services = data["services"]
    homes = [s for s in services if archived(s)]
    if any(not publication(s) for s in homes):
        raise ValueError("Archived report lacks publication date; do not substitute visit date.")
    dates = sorted(publication(s) for s in homes)
    output = args.output_dir
    folder = output / "management-reports"
    folder.mkdir(parents=True, exist_ok=True)
    configure_fonts()
    reports = [make_report(data, services, "baseline", dates[0], dates[-1])]
    last_report_month = max(dates[-1][:7], data["meta"]["snapshot_date"][:7])
    for month in month_range(dates[0][:7], last_report_month):
        year, number = map(int, month.split("-"))
        cohort = [s for s in homes if publication(s)[:7] == month]
        reports.append(make_report(data, cohort, month, month + "-01",
                                   f"{month}-{calendar.monthrange(year, number)[1]:02}"))
    library = {
        "meta": {
            "app": "CareLens Cymru", "basis": "Report publication month",
            "snapshot_date": data["meta"]["snapshot_date"],
            "captured_at": data["meta"].get("captured_at"),
            "earliest_publication_month": dates[0][:7], "latest_publication_month": dates[-1][:7],
            "latest_report_month": last_report_month,
            "cohort_basis": "Fixed all-data period cohort; latest archived report per service only",
            "source": "Care Inspectorate Wales / Welsh Government",
            "licence": "Crown copyright / Open Government Licence; logos excluded",
            "licence_url": OGL, "source_url": data["meta"].get("source_url"),
            "copyright_permission_url": COPYRIGHT,
            "licence_notice_context": "979 explicit PDF notices; The Haven uses general CIW website Crown-material reuse permission.",
            "monthly_reports": len(reports) - 1,
            "empty_months": sum(not r["summary"]["reports"] for r in reports[1:]),
        },
        "reports": reports,
    }
    for report in reports:
        write_html(report, output / report["html"], library["meta"]["snapshot_date"])
        write_pdf(report, output / report["pdf"], library["meta"]["snapshot_date"])
        print(f'{report["id"]}: {report["summary"]["reports"]} archived reports', flush=True)
    validate(library, output, data)
    (output / "report-library.json").write_text(json.dumps(library, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(library["meta"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
