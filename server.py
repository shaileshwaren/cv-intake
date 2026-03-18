"""
Manatal CV Intake — FastAPI Server
===================================
Routes:
  GET /webhook?api_key=xxx  → Processes all Pending rows in Airtable.
                              Streams an instant spinner, then a results page.
                              Triggered by Airtable button "Open URL" action.

Run locally:  uvicorn server:app --reload --port 8080
Open:         http://127.0.0.1:8080/webhook?api_key=<APP_API_KEY>
"""

import asyncio
import logging
import os

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pathlib import Path

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="Manatal CV Intake")

# ── CONFIG ────────────────────────────────────────────────────
MANATAL_API_TOKEN = os.getenv("MANATAL_API_TOKEN")
AIRTABLE_TOKEN    = os.getenv("AIRTABLE_TOKEN")
AIRTABLE_BASE_ID  = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_ID = os.getenv("AIRTABLE_TABLE_ID")
APP_API_KEY       = os.getenv("APP_API_KEY")

MANATAL_BASE  = "https://api.manatal.com/open/v3"
AIRTABLE_BASE = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}"
INTAKE_TABLE  = AIRTABLE_TABLE_ID
STAGE_ID      = 174245

MANATAL_HEADERS  = {"Authorization": f"Token {MANATAL_API_TOKEN}"}
AIRTABLE_HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_TOKEN}",
    "Content-Type":  "application/json",
}


# ── MANATAL HELPERS ───────────────────────────────────────────
def create_candidate(name: str, source: str) -> int:
    r = requests.post(
        f"{MANATAL_BASE}/candidates/",
        json={"full_name": name.strip(), "source_type": "other", "source_other": source},
        headers=MANATAL_HEADERS,
    )
    r.raise_for_status()
    return r.json()["id"]


def upload_resume(candidate_id: int, cv_url: str) -> None:
    existing = requests.get(
        f"{MANATAL_BASE}/candidates/{candidate_id}/resume/",
        headers=MANATAL_HEADERS,
    )
    if existing.status_code == 200 and existing.json().get("id"):
        resume_id = existing.json()["id"]
        requests.delete(
            f"{MANATAL_BASE}/candidates/{candidate_id}/resume/{resume_id}/",
            headers=MANATAL_HEADERS,
        )
    r = requests.post(
        f"{MANATAL_BASE}/candidates/{candidate_id}/resume/",
        json={"resume_file": cv_url},
        headers=MANATAL_HEADERS,
    )
    r.raise_for_status()


def create_match(candidate_id: int, job_id: str) -> int:
    r = requests.post(
        f"{MANATAL_BASE}/matches/",
        json={"candidate": candidate_id, "job": int(job_id), "stage": {"id": STAGE_ID}},
        headers=MANATAL_HEADERS,
    )
    r.raise_for_status()
    return r.json()["id"]


def add_note(candidate_id: int, source: str, job_id: str) -> None:
    info = (
        f"<p><strong>Source:</strong> {source}</p>"
        f"<p><strong>Job ID:</strong> {job_id}</p>"
        f"<p><strong>Uploaded via:</strong> Oxydata CV Intake</p>"
    )
    r = requests.post(
        f"{MANATAL_BASE}/candidates/{candidate_id}/notes/",
        json={"info": info},
        headers=MANATAL_HEADERS,
    )
    r.raise_for_status()


# ── AIRTABLE HELPERS ──────────────────────────────────────────
def get_pending_records() -> list:
    r = requests.get(
        f"{AIRTABLE_BASE}/{INTAKE_TABLE}",
        headers=AIRTABLE_HEADERS,
        params={"filterByFormula": "{Status}='Pending'"},
    )
    r.raise_for_status()
    return r.json().get("records", [])


def update_record(record_id: str, fields: dict) -> None:
    r = requests.patch(
        f"{AIRTABLE_BASE}/{INTAKE_TABLE}/{record_id}",
        headers=AIRTABLE_HEADERS,
        json={"fields": fields},
    )
    r.raise_for_status()


# ── CORE PROCESSING LOGIC ─────────────────────────────────────
def process_record(rec: dict) -> dict:
    fields      = rec.get("fields", {})
    record_id   = rec["id"]
    name        = fields.get("Name", "").strip()
    job_id      = str(fields.get("Job ID", "")).strip()
    source      = fields.get("Source", "Monster")
    attachments = fields.get("CV", [])

    result = {
        "name":         name or "(no name)",
        "job_id":       job_id,
        "source":       source,
        "status":       "failed",
        "candidate_id": None,
        "error":        None,
    }

    if not name:
        update_record(record_id, {"Status": "Failed", "Notes": "Name is empty"})
        result["error"] = "Name is empty"
        log.warning("FAIL  (no name) — Name is empty")
        return result

    if not job_id or job_id == "None":
        update_record(record_id, {"Status": "Failed", "Notes": "Job ID is empty"})
        result["error"] = "Job ID is empty"
        log.warning("FAIL  %s — Job ID is empty", name)
        return result

    if not attachments:
        update_record(record_id, {"Status": "Failed", "Notes": "No CV attached"})
        result["error"] = "No CV attached"
        log.warning("FAIL  %s — No CV attached", name)
        return result

    cv_url  = attachments[0].get("url")
    cv_name = attachments[0].get("filename", "cv.pdf")

    try:
        cid = create_candidate(name, source)
        result["candidate_id"] = cid
        log.info("CAND  %s → ID %s", name, cid)

        resume_status = "uploaded"
        try:
            upload_resume(cid, cv_url)
            log.info("CV    %s → %s", name, cv_name)
        except Exception as e:
            resume_status = f"failed: {str(e)[:80]}"
            log.warning("CV    %s — upload failed: %s", name, e)

        create_match(cid, job_id)
        log.info("MATCH %s → job %s", name, job_id)

        try:
            add_note(cid, source, job_id)
        except Exception as e:
            log.warning("NOTE  %s — failed: %s", name, e)

        update_record(record_id, {
            "Status":       "Uploaded",
            "Candidate ID": cid,
            "Notes":        f"CV: {resume_status} | File: {cv_name}",
        })
        result["status"] = "success"
        log.info("DONE  %s — success", name)

    except requests.HTTPError as e:
        msg = f"{e.response.status_code}: {e.response.text[:150]}"
        result["error"] = msg
        update_record(record_id, {"Status": "Failed", "Notes": msg})
        log.error("FAIL  %s — HTTP %s", name, msg)

    except Exception as e:
        result["error"] = str(e)[:150]
        update_record(record_id, {"Status": "Failed", "Notes": str(e)[:150]})
        log.error("FAIL  %s — %s", name, e)

    return result


# ── HTML BUILDERS ─────────────────────────────────────────────
_PAGE_STYLES = """
  body  { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          background: #f9fafb; color: #111827; margin: 0; padding: 40px 24px; }
  h1    { font-size: 20px; font-weight: 700; margin: 0 0 4px }
  .sub  { color: #6b7280; font-size: 14px; margin-bottom: 24px }
  .stats{ display: flex; gap: 16px; margin-bottom: 24px; flex-wrap: wrap }
  .stat { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px;
          padding: 12px 20px; min-width: 100px }
  .stat-n { font-size: 28px; font-weight: 700 }
  .stat-l { font-size: 12px; color: #6b7280; margin-top: 2px }
  table { width: 100%; border-collapse: collapse; background: #fff;
          border: 1px solid #e5e7eb; border-radius: 8px; overflow: hidden }
  th    { background: #f3f4f6; text-align: left; padding: 10px 12px;
          font-size: 12px; text-transform: uppercase; letter-spacing: .05em;
          color: #6b7280; border-bottom: 1px solid #e5e7eb }
  @keyframes spin { to { transform: rotate(360deg) } }
  .spinner { width: 36px; height: 36px; border: 3px solid #e5e7eb;
             border-top-color: #6b7280; border-radius: 50%;
             animation: spin .8s linear infinite; margin-bottom: 16px }
"""


def build_spinner_html(count: int) -> str:
    noun = "record" if count == 1 else "records"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>CV Intake — Processing</title>
  <style>{_PAGE_STYLES}</style>
</head>
<body>
  <div id="spinner-view">
    <div class="spinner"></div>
    <h1>Processing {count} {noun}...</h1>
    <p class="sub">Uploading to Manatal — please wait</p>
  </div>
  <div id="results-view" style="display:none"></div>
"""


def build_results_html(results: list) -> str:
    success = sum(1 for r in results if r["status"] == "success")
    failed  = sum(1 for r in results if r["status"] == "failed")
    total   = len(results)
    summary_color = "#16a34a" if failed == 0 else "#dc2626"

    rows = ""
    for r in results:
        if r["status"] == "success":
            badge = '<span style="color:#16a34a;font-weight:600">&#10003; Uploaded</span>'
        else:
            badge = '<span style="color:#dc2626;font-weight:600">&#10007; Failed</span>'
        detail = r["error"] or (f"Candidate ID: {r['candidate_id']}" if r["candidate_id"] else "")
        rows += f"""
        <tr>
          <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb">{r['name']}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb">{r['job_id']}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb">{r['source']}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb">{badge}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;color:#6b7280;font-size:13px">{detail}</td>
        </tr>"""

    results_markup = f"""
  <div id="results-content" style="display:none">
    <h1>CV Intake — Run Complete</h1>
    <p class="sub">Processed {total} pending record(s)</p>
    <div class="stats">
      <div class="stat">
        <div class="stat-n" style="color:{summary_color}">{success}</div>
        <div class="stat-l">Uploaded</div>
      </div>
      <div class="stat">
        <div class="stat-n" style="color:#dc2626">{failed}</div>
        <div class="stat-l">Failed</div>
      </div>
    </div>
    <table>
      <thead>
        <tr><th>Name</th><th>Job ID</th><th>Source</th><th>Status</th><th>Detail</th></tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
  </div>
  <script>
    document.getElementById('spinner-view').style.display = 'none';
    var rv = document.getElementById('results-view');
    rv.innerHTML = document.getElementById('results-content').innerHTML;
    rv.style.display = 'block';
  </script>
</body>
</html>"""

    return results_markup


# ── ROUTE ─────────────────────────────────────────────────────
@app.get("/webhook")
async def webhook(
    api_key: str | None = Query(default=None),
    x_api_key: str | None = Header(default=None),
):
    """Triggered by Airtable button. Streams a spinner instantly, then the results page."""

    if APP_API_KEY:
        provided = api_key or x_api_key
        if provided != APP_API_KEY:
            raise HTTPException(status_code=401, detail="Unauthorized")

    log.info("WEBHOOK triggered")

    async def generate():
        try:
            records = await asyncio.to_thread(get_pending_records)
        except Exception as e:
            log.error("Failed to fetch Airtable records: %s", e)
            yield f"""<!DOCTYPE html><html><body>
                <p style="color:red;font-family:sans-serif">
                  Failed to fetch Airtable records: {e}
                </p></body></html>"""
            return

        log.info("Found %d pending record(s)", len(records))

        # Phase 1 — spinner (sent immediately)
        yield build_spinner_html(len(records))

        if not records:
            yield build_results_html([])
            return

        # Phase 2 — process all records in parallel, then stream results
        results = list(await asyncio.gather(
            *[asyncio.to_thread(process_record, rec) for rec in records]
        ))

        success = sum(1 for r in results if r["status"] == "success")
        failed  = sum(1 for r in results if r["status"] == "failed")
        log.info("WEBHOOK done — %d success, %d failed", success, failed)

        yield build_results_html(results)

    return StreamingResponse(generate(), media_type="text/html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8080, reload=True)
