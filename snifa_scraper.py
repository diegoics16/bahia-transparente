"""
SNIFA facility compliance scraper — Bahía Quintero-Puchuncaví

Built from real page structure (inspected directly from saved HTML on
2026-07-06), not guessed from click recordings. Key facts this relies on:

- Results table is #tResultado, columns: # | Unidad Fiscalizable |
  Nombre Razon Social | Región | Categoría | Comuna | Detalle
- Región/Comuna filters are bootstrap-select widgets with a REAL
  <select> underneath (ddlRegion, ddlComuna) — set values directly
  instead of clicking the visual dropdown.
- "Buscar" button just calls a JS function buscar() — call it directly.
- Results are paginated. Setting page length to 100 does NOT mean you
  see everything if there are more than 100 matches — must page through.
- Each facility detail page has 5 real HTML tables (not PDFs) with
  actual status columns:
    #procedimientos-sancionatorios : Rol | Fecha Inicio | Estado
    #sanciones                     : (often empty — plain text if so)
    #fiscalizaciones               : Expediente | Año actividad | Estado
    #medidas-provisionales         : Rol | Fecha de Inicio | Estado
    #seguimientos                  : Fecha Informe | RCA Asociada |
                                      SubComponente Ambiental | Categoría
                                      | Frecuencia

None of this requires opening a single PDF. The "Ver detalle" links
inside each row go to individual PDFs — this script does not follow
those; the table row itself already has the plain-language signal.

WHAT THIS DELIBERATELY EXCLUDES
- ESVAL: reports to SISS, not SMA/SNIFA. Track separately.
- Individual report PDFs — out of scope, not needed for the top-line
  compliance status the dashboard shows.

CHANGES FOR SCHEDULED / CI USE (2026-07-08)
- headless is now controlled by the SNIFA_HEADFUL env var instead of
  being hardcoded — CI has no display, so it must run headless. Set
  SNIFA_HEADFUL=1 locally if you want to watch the browser for debugging.
- Added write-to-Supabase step. Requires SUPABASE_URL and
  SUPABASE_SERVICE_KEY as environment variables. The service-role key
  is used here (server-side, in CI) because it bypasses Row Level
  Security — never put the service-role key in the HTML/frontend.
- Added a sync_runs log entry for every run, success or failure, so a
  broken scraper shows up as a row in the database instead of just a
  stale map nobody notices for three weeks.
- The local snifa_snapshot.json file is still written every run — the
  DB write is in addition to that, not instead of it. If the DB write
  fails, you still have the raw snapshot to inspect or replay.
- Facility date fields ("Fecha Inicio", "Fecha Informe", etc.) are
  NOT auto-parsed into SQL dates yet. SNIFA's actual date string format
  hasn't been confirmed across all five sections, and past experience
  on this project (POAL) shows guessing at date/number formats causes
  silent corruption. Dates are stored as text in the `raw` jsonb column
  only for now; parsing them into the `fecha` column is a follow-up
  once the real format is confirmed against live data.

USAGE
    python -m pip install playwright requests
    playwright install chromium
    python snifa_scraper.py
    -> writes snifa_snapshot.json locally AND upserts into Supabase
"""

import json
import os
import re
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright
import requests

SNIFA_URL = "https://snifa.sma.gob.cl/UnidadFiscalizable"

TARGET_FACILITIES = ["CODELCO", "ENAP", "AES ANDES"]

REGION_VALUE = "6"  # Región de Valparaíso
COMUNA_VALUES = ["108", "114"]  # Puchuncaví, Quintero — dropped Valparaíso commune,
# which is the municipal-sewage story (UPLA/Loma Larga), not this one, and was
# the main reason the count sat at 175 across 2 pages.

SECTIONS = {
    "titular": "Titular",
    "instrumentos-aplicables": "Instrumentos Aplicables",
    "procedimientos-sancionatorios": "Procedimientos Sancionatorios",
    "sanciones": "Sanciones",
    "fiscalizaciones": "Fiscalizaciones",
    "medidas-provisionales": "Medidas Provisionales",
    "requerimiento-ingreso": "Requerimientos de Ingreso",
    "seguimientos": "Seguimiento Ambiental",
}

# Which sections become rows in the snifa_events table.
EVENT_SECTIONS = ["Procedimientos Sancionatorios", "Fiscalizaciones", "Medidas Provisionales"]

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
HEADLESS = os.environ.get("SNIFA_HEADFUL", "0") != "1"


# ──────────────────────────────────────────────────────────
# Supabase writes
# ──────────────────────────────────────────────────────────
def supabase_write(table, rows, on_conflict=None):
    """POST rows to a Supabase table via its REST API using the
    service-role key. Raises loudly on failure instead of swallowing
    the error — a silent failure here is worse than a crashed CI run,
    because a crashed run at least sends you an email."""
    if not rows:
        return
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise RuntimeError(
            "SUPABASE_URL / SUPABASE_SERVICE_KEY are not set. Set them as "
            "environment variables (local run) or GitHub Actions secrets (CI)."
        )
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    if on_conflict:
        url += f"?on_conflict={on_conflict}"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": ("resolution=merge-duplicates,return=minimal" if on_conflict else "return=minimal"),
    }
    resp = requests.post(url, headers=headers, json=rows, timeout=60)
    if resp.status_code >= 300:
        raise RuntimeError(f"Supabase write to {table} failed ({resp.status_code}): {resp.text}")


def log_sync_run(started_at, finished_at, status, rows_written, error=None):
    """Best-effort log entry — if this itself fails, print a warning
    but don't let it hide the real success/failure of the scrape."""
    try:
        supabase_write("sync_runs", [{
            "source": "snifa",
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "status": status,
            "rows_written": rows_written,
            "error": error,
        }])
    except Exception as e:
        print(f"WARNING: could not write sync_runs log entry: {e}")


def facility_row(match, detail):
    return {
        "id": detail["detail_url"],           # detail_url is stable and unique — used as the primary key
        "razon_social": match.get("razon_social"),
        "comuna": match.get("comuna"),
        "detail_url": detail["detail_url"],
        "last_scraped_at": detail["retrieved_at"],
    }


def event_rows(detail):
    rows = []
    for label in EVENT_SECTIONS:
        section = detail.get(label, {})
        for r in section.get("rows", []):
            rows.append({
                "facility_id": detail["detail_url"],
                "section": label,
                "rol_or_expediente": r.get("Rol") or r.get("Expediente"),
                "fecha": None,  # see module docstring — not parsed yet, raw text lives in `raw`
                "estado": r.get("Estado"),
                "raw": r,
            })
    return rows


# ──────────────────────────────────────────────────────────
# Scraping
# ──────────────────────────────────────────────────────────
def apply_filters(page):
    page.goto(SNIFA_URL)
    page.select_option("select#ddlRegion", REGION_VALUE)
    page.select_option("select#ddlComuna", COMUNA_VALUES)
    page.evaluate("buscar()")
    page.wait_for_timeout(2000)

    # Page-length selector is an optimization, not a requirement — the
    # pagination loop in extract_all_rows() walks every page regardless
    # of page size. If this fails (e.g. fewer results changes how the
    # table renders), don't crash the whole run over it — log it and
    # save a screenshot so we can actually see what's on the page
    # instead of guessing again.
    try:
        page.select_option("select#tResultado_length", "100", timeout=5000)
        page.wait_for_timeout(1000)
    except Exception as e:
        print(f"Could not set page length to 100, continuing without it: {e}")
        page.screenshot(path="debug_after_buscar.png", full_page=True)
        print("Saved debug_after_buscar.png — send this back if row extraction also fails.")


def extract_all_rows(page):
    """Walk every results page, not just the first, using the real
    #tResultado table columns."""
    all_rows = []
    seen_pages = 0
    while True:
        seen_pages += 1
        trs = page.query_selector_all("table#tResultado tbody tr")
        for tr in trs:
            tds = tr.query_selector_all("td")
            if len(tds) < 7:
                continue
            link = tds[6].query_selector("a")
            all_rows.append({
                "unidad_fiscalizable": tds[1].inner_text().strip(),
                "razon_social": tds[2].inner_text().strip(),
                "comuna": tds[5].inner_text().strip(),
                "href": link.get_attribute("href") if link else None,
            })

        next_btn = page.query_selector("a.paginate_button.next")
        if not next_btn or "disabled" in (next_btn.get_attribute("class") or ""):
            break
        next_btn.click()
        page.wait_for_timeout(1500)

    print(f"Walked {seen_pages} result page(s), {len(all_rows)} facilities total.")
    return all_rows


def cell_text(el):
    """Extract text from a table cell, treating <br> as a separator
    instead of silently dropping it. inner_text() collapses <br> tags
    with no space, which turns multi-value cells (e.g. a Subcomponente
    column listing 'Aguas marinas<br>Sedimentos<br>Otros') into an
    unreadable run-together string. Confirmed against real SNIFA HTML
    on 2026-07-06 before this fix went in."""
    html = el.inner_html()
    html = re.sub(r"<br\s*/?>", "; ", html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", html)
    return re.sub(r"\s+", " ", text).strip()


def extract_section_table(page, section_id):
    """Pull a section's real table rows as dicts keyed by header text.
    If the section is empty, SNIFA shows plain text instead of a table
    — captured as a note rather than treated as an error."""
    div = page.query_selector(f"div#{section_id}")
    if not div:
        return {"rows": [], "note": "section not found on this page"}

    table = div.query_selector("table")
    if not table:
        return {"rows": [], "note": div.inner_text().strip()}

    headers = [cell_text(th) for th in table.query_selector_all("th")]
    rows = []
    for tr in table.query_selector_all("tbody tr"):
        cells = [cell_text(td) for td in tr.query_selector_all("td")]
        if cells:
            rows.append(dict(zip(headers, cells)))
    return {"rows": rows, "note": None}


def extract_facility_detail(page, href):
    url = href if href.startswith("http") else f"https://snifa.sma.gob.cl{href}"
    page.goto(url)
    page.wait_for_timeout(1500)

    name_el = page.query_selector("h1, h2, h3")
    detail = {
        "detail_url": page.url,
        "facility_name": name_el.inner_text().strip() if name_el else None,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "source": "SNIFA",
    }
    for section_id, label in SECTIONS.items():
        detail[label] = extract_section_table(page, section_id)
    return detail


def plain_language(detail):
    """Turn the real status columns into the kind of line the dashboard
    can show without any translation guesswork — these are real Estado
    values pulled from the page, not inferred."""
    lines = []
    proc = detail.get("Procedimientos Sancionatorios", {})
    if proc.get("rows"):
        for r in proc["rows"]:
            lines.append(f"Sanction procedure {r.get('Rol','?')} (started {r.get('Fecha Inicio','?')}): {r.get('Estado','?')}")
    elif proc.get("note"):
        lines.append(proc["note"])

    seg = detail.get("Seguimiento Ambiental", {})
    if seg.get("rows"):
        # Each row's value may itself be multiple values joined by "; "
        # (from the <br>-separated cell fix) — split before deduping,
        # otherwise "Aguas marinas; Sedimentos; Otros" as a whole string
        # never matches another row that has the same three values in
        # a different order or combination.
        all_subs = set()
        for r in seg["rows"]:
            raw = r.get("SubComponente Ambiental", "")
            for part in raw.split(";"):
                part = part.strip()
                if part:
                    all_subs.add(part)
        lines.append(f"{len(seg['rows'])} monitoring reports on file, covering: {', '.join(sorted(all_subs))}")

    instr = detail.get("Instrumentos Aplicables", {})
    if instr.get("rows"):
        names = [r.get("Nombre", "") for r in instr["rows"] if r.get("Nombre")]
        lines.append(f"{len(instr['rows'])} applicable permits/RCAs on file, including: {'; '.join(names[:3])}{'...' if len(names) > 3 else ''}")

    return lines


def main():
    started_at = datetime.now(timezone.utc)
    total_rows_written = 0

    snapshot = {
        "generated_at": started_at.isoformat(),
        "scope": {
            "region": "Región de Valparaíso",
            "comunas": ["Puchuncaví", "Quintero"],
            "facilities_targeted": TARGET_FACILITIES,
            "excludes": "ESVAL (reports to SISS, not SMA/SNIFA — track separately)",
        },
        "facilities": [],
    }

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=HEADLESS)
            page = browser.new_page()

            apply_filters(page)
            rows = extract_all_rows(page)

            matched = [
                r for r in rows
                if any(t.lower() in (r["unidad_fiscalizable"] + " " + r["razon_social"]).lower()
                       for t in TARGET_FACILITIES)
            ]
            print(f"\nMatched {len(matched)} target facilities:")
            for m in matched:
                print(f"  - {m['unidad_fiscalizable']}  ({m['razon_social']})")

            all_facility_rows = []
            all_event_rows = []

            for m in matched:
                detail = extract_facility_detail(page, m["href"])
                detail["plain_language"] = plain_language(detail)
                snapshot["facilities"].append(detail)
                all_facility_rows.append(facility_row(m, detail))
                all_event_rows.extend(event_rows(detail))
                print(f"Pulled: {detail['facility_name']}")

            browser.close()

        # Local file first — this is your safety net if the DB write below fails.
        with open("snifa_snapshot.json", "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        print(f"\nWrote snifa_snapshot.json with {len(snapshot['facilities'])} facilities.")

        # Now push to Supabase.
        print("\nWriting to Supabase...")
        supabase_write("snifa_facilities", all_facility_rows, on_conflict="id")
        print(f"  upserted {len(all_facility_rows)} facilities")
        supabase_write("snifa_events", all_event_rows)
        print(f"  inserted {len(all_event_rows)} events")
        total_rows_written = len(all_facility_rows) + len(all_event_rows)

        log_sync_run(started_at, datetime.now(timezone.utc), "success", total_rows_written)
        print("\nDone.")

    except Exception as e:
        log_sync_run(started_at, datetime.now(timezone.utc), "failed", total_rows_written, error=str(e))
        print(f"\nSCRAPE FAILED: {e}")
        raise  # re-raise so the GitHub Actions run shows as failed and emails you


if __name__ == "__main__":
    main()
