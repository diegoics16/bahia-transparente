"""
Build the clean POAL dataset for Bahia Transparente.

CONFIRMED against real data (tested on 2026-07-07, both against the live
DIRECTEMAR download and against a 34,873-row real sample pulled from it):
  - ZIP contains one file: POAL_estandarizado_2024.csv
  - Read with sep=';', encoding='latin-1' (utf-8 and comma-sep both fail)
  - 343,859 total rows / 22 columns in the full national dataset
  - Numeric fields (VALOR Corregida, coordinates) use comma as decimal
    separator and must be converted before pd.to_numeric
  - Dates are DD-MM-YYYY strings
  - Estación.POAL (station code) is the stable join key. TOPONIMO
    (station name text) drifts across years for the same code - e.g.
    "Caleta Ventanas" vs "Caleta Ventana", "Muelle ENAP (RPC)" vs
    "Muelle ERA (RPC)" - do not join or group on TOPONIMO.
  - ~13% of Quintero rows share the same station+parametro+fecha with
    differing values (likely QA/QC field replicates, cause not
    confirmed). In the sample tested, none of these duplicate groups
    had conflicting units, so straight averaging is safe THERE - the
    script still checks for unit conflicts on the full dataset in case
    that changes once Puchuncavi/other bodies are included.
  - Report title says data runs "1993-2023"; zip filename says "2024".
    Confirmed empirically: data actually goes through 2024.

ASSUMED (flagged, not silently resolved):
  - Dedup aggregation = mean of numeric values within a
    (Cuerpo.de.Agua, MATRIZ, Estación.POAL, Parametro, Fecha) group.
    If you'd rather keep "latest lab report" or "first" instead of
    mean, change AGG_METHOD below.
  - CRITERIO column (NADA/MEDIA) meaning is NOT confirmed. It is
    carried through to the output as-is but not used for any logic.
  - "PUCHUNCAVI" was NOT confirmed as a distinct Cuerpo.de.Agua in the
    dataset - station names like "Caleta Ventanas" and "Frente CT
    Nueva Ventanas" (the Ventanas industrial complex) already appear
    grouped under Cuerpo.de.Agua = QUINTERO. The keyword list below
    includes "puchuncavi" defensively; the script will print a loud
    warning if it actually finds a separate entity, since that would
    change how we frame Quintero vs Puchuncavi as one bay or two.

OUTPUTS (all written next to this script):
  1. poal_clean_quintero_bahia.csv   - one row per station/parameter/date,
     ready to feed the dashboard. Includes lat/lon resolved from each
     station's own history (see coord_source column: "station_canonical"
     = recovered/confirmed, "row_reported_unverified" = only one
     in-bounds reading ever seen for this station, take with caution,
     "unresolved" = no valid coordinate found anywhere, NaN on purpose)
  2. poal_station_name_variants.csv  - stations where TOPONIMO text
     disagrees across years for the same station code (manual review)
  3. poal_coordinate_issues.csv      - stations that are either (a)
     geographic outliers vs. their water body's other stations even
     after resolution, or (b) have no valid coordinate anywhere in
     the data. Confirmed on real data: 040-A-Qu and 122-S-Qu are both
     outlier cases where every single reading agrees with itself but
     disagrees with the rest of Quintero - can't be fixed by cross-
     referencing this dataset alone, needs manual verification.
  4. poal_value_parse_failures.csv   - rows where VALOR Corregida itself
     didn't parse as a number (same digit-grouping corruption pattern
     as the coordinates, e.g. "1.080.466.662" for a Cadmio Total
     reading in mg/kg). Excluded from the clean dataset, not guessed at.
  5. poal_unit_conflicts.csv         - only created if a duplicate
     group has more than one unit for the same station/parametro/date
     (none found in the 4-body sample tested; full national data may differ)

CHANGES FOR SCHEDULED / CI USE (2026-07-08)
  - Added a Supabase push step at the end of main(). Only rows for
    SUPABASE_PUSH_BODIES (the bay itself + real comparison sites) are
    pushed to the live database — the full national CSV is still
    written locally in full, since it's useful for any future
    comparison, but pushing all 64 water bodies to a small pilot
    database on every run is unnecessary scope creep.
  - Added a sync_runs log entry, same pattern as snifa_scraper.py.
  - Requires SUPABASE_URL and SUPABASE_SERVICE_KEY as environment
    variables (local run) or GitHub Actions secrets (CI).

Run:
    python -m pip install requests pandas
    python build_poal_dataset.py
"""

import os
import zipfile
from datetime import datetime, timezone
import numpy as np
import pandas as pd
import requests

ZIP_URL = "https://www.directemar.cl/directemar/site/docs/20260114/20260114164231/poal_estandarizado_2024.zip"
DOWNLOAD_DIR = "downloads"
EXTRACT_DIR = "poal_estandarizado_extracted"

# INCLUDE_ALL_BODIES=True builds the full national dataset (all 64 water
# bodies DIRECTEMAR publishes) through the same cleaning pipeline, so any
# future comparison - Quintay, other bays, whatever comes up - is already
# in the file without re-running anything. Set False to go back to the
# Quintero-region-only subset from before.
INCLUDE_ALL_BODIES = True

# Only used when INCLUDE_ALL_BODIES=False
LOCATION_KEYWORDS = ["quintero", "concon", "concón", "valparaiso", "valparaíso",
                      "playa ancha", "puchuncavi", "puchuncaví"]

# What actually gets pushed to the live Supabase database, regardless of
# INCLUDE_ALL_BODIES. Keep this narrow and deliberate — the dashboard is
# about this bay, not a national POAL browser. Add "QUINTAY" here once/if
# it's confirmed to exist as its own Cuerpo.de.Agua (see the loud warning
# filter_location() prints about this).
SUPABASE_PUSH_BODIES = ["QUINTERO", "PUCHUNCAVI", "PUCHUNCAVÍ", "QUINTAY", "ZAPALLAR"]

AGG_METHOD = "mean"  # ASSUMED - see docstring. Alternative: "latest"
COORD_OUTLIER_THRESHOLD_DEG = 0.3

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
SUPABASE_BATCH_SIZE = 500  # rows per POST — keeps payloads well under any request-size limit


def download_zip() -> str:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    dest = os.path.join(DOWNLOAD_DIR, "poal_estandarizado_2024.zip")
    print(f"Downloading {ZIP_URL} ...")
    r = requests.get(ZIP_URL, timeout=120)
    r.raise_for_status()
    with open(dest, "wb") as f:
        f.write(r.content)
    print(f"  saved {dest} ({len(r.content)/1024:.1f} KiB)")
    return dest


def extract_csv(zip_path: str) -> str:
    os.makedirs(EXTRACT_DIR, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise RuntimeError(f"No CSV found in zip. Contents were: {zf.namelist()}")
        if len(names) > 1:
            print(f"  WARNING: multiple CSVs found, using first: {names}")
        zf.extractall(EXTRACT_DIR)
        csv_path = os.path.join(EXTRACT_DIR, names[0])
        print(f"  extracted {csv_path}")
        return csv_path


def load_raw(csv_path: str) -> pd.DataFrame:
    # CONFIRMED separator/encoding - do not need the fallback loop from the
    # exploration script anymore, but keep one fallback in case DIRECTEMAR
    # changes the export format without notice.
    try:
        df = pd.read_csv(csv_path, sep=";", encoding="latin-1", low_memory=False)
    except Exception as e:
        print(f"  Confirmed read method failed ({e}), trying utf-8/comma fallback...")
        df = pd.read_csv(csv_path, sep=",", encoding="utf-8", low_memory=False)
    print(f"  loaded {df.shape[0]} rows x {df.shape[1]} columns (national, all water bodies)")
    return df


def filter_location(df: pd.DataFrame) -> pd.DataFrame:
    if INCLUDE_ALL_BODIES:
        all_bodies = sorted(df["Cuerpo.de.Agua"].dropna().unique())
        print(f"  INCLUDE_ALL_BODIES=True - keeping all {len(df)} rows across "
              f"{len(all_bodies)} water bodies nationally")
        if any("quintay" in b.lower() for b in all_bodies):
            print("  *** QUINTAY appears as its own Cuerpo.de.Agua - this is the CurazLeiva ***")
            print("  *** paper's actual control site, distinct from Zapallar. Check it.      ***")
        else:
            print("  Quintay does NOT appear as a separate Cuerpo.de.Agua in POAL either - "
                  "confirms neither Zapallar nor Quintay has DIRECTEMAR monitoring.")
        return df.copy()

    mask = df["Cuerpo.de.Agua"].astype(str).str.lower().apply(
        lambda v: any(kw in v for kw in LOCATION_KEYWORDS)
    )
    sub = df[mask].copy()
    found_bodies = sorted(sub["Cuerpo.de.Agua"].unique())
    print(f"  filtered to {len(sub)} rows across water bodies: {found_bodies}")
    if any("puchuncav" in b.lower() for b in found_bodies):
        print("  *** NOTE: PUCHUNCAVI appears as its OWN Cuerpo.de.Agua entity. ***")
        print("  *** This was not confirmed when this script was written -    ***")
        print("  *** check whether it should be merged with QUINTERO or kept  ***")
        print("  *** separate before using it in the fragmentation narrative. ***")
    return sub


def clean_types(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["valor_num"] = pd.to_numeric(
        df["VALOR Corregida"].astype(str).str.replace(",", ".", regex=False), errors="coerce"
    )
    df["lat"] = pd.to_numeric(
        df["Coordenada.Y.POAL"].astype(str).str.replace(",", ".", regex=False), errors="coerce"
    )
    df["lon"] = pd.to_numeric(
        df["Coordenada.X.POAL"].astype(str).str.replace(",", ".", regex=False), errors="coerce"
    )
    df["fecha_muestreo"] = pd.to_datetime(df["Fecha.de.Muestreo"], format="%d-%m-%Y", errors="coerce")

    n_bad_val = df["valor_num"].isna().sum()
    n_bad_date = df["fecha_muestreo"].isna().sum()
    if n_bad_val or n_bad_date:
        print(f"  WARNING: {n_bad_val} rows failed numeric value parse, "
              f"{n_bad_date} rows failed date parse - inspect before dropping.")
    return df


def flag_station_name_variants(df: pd.DataFrame) -> pd.DataFrame:
    """For each station code, find the most common TOPONIMO and flag any
    code that has more than one distinct name across the dataset.

    BUGFIX (found running the national dataset for real): some stations
    have NO non-null TOPONIMO anywhere in their rows. value_counts() drops
    NaN by default, so idxmax() on an all-null group crashed with
    "attempt to get argmax of an empty sequence". Handled explicitly now -
    canonical name becomes None for these, not a crash.
    """
    def _canonical(s):
        non_null = s.dropna()
        if len(non_null) == 0:
            return None
        return non_null.value_counts().idxmax()

    name_stats = (
        df.groupby("Estación.POAL")["TOPONIMO"]
        .agg(_canonical)
        .rename("toponimo_canonico")
    )
    variant_counts = df.groupby("Estación.POAL")["TOPONIMO"].nunique()  # nunique() also drops NaN by default
    variants = variant_counts[variant_counts > 1]
    no_name_stations = name_stats[name_stats.isna()]

    report_rows = []
    for code in variants.index:
        names = df.loc[df["Estación.POAL"] == code, "TOPONIMO"].value_counts()
        report_rows.append({
            "Estación.POAL": code,
            "n_variants": len(names),
            "names_and_counts": "; ".join(f"{n} ({c})" for n, c in names.items()),
        })
    variant_report = pd.DataFrame(report_rows)
    print(f"  {len(variants)} of {df['Estación.POAL'].nunique()} stations have inconsistent TOPONIMO text")
    if len(no_name_stations):
        print(f"  WARNING: {len(no_name_stations)} stations have NO TOPONIMO value anywhere "
              f"(codes: {list(no_name_stations.index)}) - toponimo will be blank for these, "
              f"not guessed at")

    df = df.merge(name_stats, on="Estación.POAL", how="left")
    df["toponimo_flag"] = df["Estación.POAL"].isin(variants.index)
    return df, variant_report


CHILE_LAT_RANGE = (-56, -17)
CHILE_LON_RANGE = (-76, -66)


def _in_bounds(lat, lon):
    return lat.between(*CHILE_LAT_RANGE) & lon.between(*CHILE_LON_RANGE)


def resolve_station_coordinates(df: pd.DataFrame):
    """Monitoring stations are fixed physical points, so a station's
    coordinate should not vary by year. CONFIRMED against real data:
    some rows have coordinates mangled by an apparent decimal-point
    corruption (e.g. "-32.91528" exported as "-3.291.528", which
    pd.to_numeric correctly refuses to parse -> NaN). Rather than guess
    at un-mangling the corrupted string, this recovers the coordinate
    from a DIFFERENT, valid-looking row for the same station code where
    one exists - tested against the real sample: 31 of 40 affected
    stations had at least one clean reading elsewhere in their own
    history and were recovered this way; 9 had none and are reported
    as unresolved, not guessed at.

    Returns:
      df with lat_final/lon_final/coord_source columns added
      issues: one row per station with a data quality problem, for
        manual review (either no valid reading anywhere, or the
        station's own coordinate disagrees with its neighbors)
    """
    df = df.copy()
    df["coord_in_bounds"] = _in_bounds(df["lat"], df["lon"])

    valid = df[df["coord_in_bounds"]].copy()
    valid["lat_r"] = valid["lat"].round(6)
    valid["lon_r"] = valid["lon"].round(6)

    canonical = (
        valid.groupby(["Cuerpo.de.Agua", "Estación.POAL"])
        .apply(lambda g: g[["lat_r", "lon_r"]].value_counts().idxmax(), include_groups=False)
        .apply(pd.Series)
        .rename(columns={0: "lat_canonical", 1: "lon_canonical"})
        .reset_index()
    )
    n_readings = (
        valid.groupby(["Cuerpo.de.Agua", "Estación.POAL"]).size().rename("n_valid_readings").reset_index()
    )
    canonical = canonical.merge(n_readings, on=["Cuerpo.de.Agua", "Estación.POAL"])

    df = df.merge(canonical, on=["Cuerpo.de.Agua", "Estación.POAL"], how="left")

    has_canonical = df["lat_canonical"].notna()

    # BUGFIX (caught by testing against real data): when neither a
    # canonical value nor an in-bounds reported value exists, this must
    # be NaN - not silently fall back to the corrupted/out-of-bounds raw
    # value (e.g. 122-S-Qu's -327.718 was leaking through here before).
    df["lat_final"] = np.select(
        [has_canonical, df["coord_in_bounds"]],
        [df["lat_canonical"], df["lat"]],
        default=np.nan,
    )
    df["lon_final"] = np.select(
        [has_canonical, df["coord_in_bounds"]],
        [df["lon_canonical"], df["lon"]],
        default=np.nan,
    )
    df["coord_source"] = np.where(
        has_canonical, "station_canonical",
        np.where(df["coord_in_bounds"], "row_reported_unverified", "unresolved")
    )

    n_unresolved_rows = (df["coord_source"] == "unresolved").sum()
    n_unresolved_stations = df.loc[df["coord_source"] == "unresolved", "Estación.POAL"].nunique()
    print(f"  {n_unresolved_rows} rows across {n_unresolved_stations} stations have NO valid "
          f"coordinate anywhere in this data - left as NaN, not guessed at")

    # flag stations whose own canonical position is a geographic outlier
    # relative to the rest of their water body (e.g. 040-A-Qu, 122-S-Qu)
    station_level = canonical.copy()
    med = station_level.groupby("Cuerpo.de.Agua")[["lat_canonical", "lon_canonical"]].transform("median")
    station_level["lat_dev"] = (station_level["lat_canonical"] - med["lat_canonical"]).abs()
    station_level["lon_dev"] = (station_level["lon_canonical"] - med["lon_canonical"]).abs()
    station_level["issue_type"] = np.where(
        (station_level["lat_dev"] > COORD_OUTLIER_THRESHOLD_DEG)
        | (station_level["lon_dev"] > COORD_OUTLIER_THRESHOLD_DEG),
        "outlier_vs_water_body_median", None
    )
    outliers = station_level[station_level["issue_type"].notna()].copy()

    unresolved = (
        df.loc[df["coord_source"] == "unresolved", ["Cuerpo.de.Agua", "Estación.POAL"]]
        .drop_duplicates()
    )
    unresolved["issue_type"] = "no_valid_coordinate_anywhere"

    issues = pd.concat([
        outliers[["Cuerpo.de.Agua", "Estación.POAL", "lat_canonical", "lon_canonical",
                   "n_valid_readings", "issue_type"]],
        unresolved,
    ], ignore_index=True)
    print(f"  {len(outliers)} stations have a resolved coordinate that's a geographic "
          f"outlier vs. their water body's other stations - needs manual verification, "
          f"not auto-corrected")

    return df, issues


def dedupe(df: pd.DataFrame):
    key = ["Cuerpo.de.Agua", "MATRIZ", "Estación.POAL", "Parámetro", "fecha_muestreo"]

    unit_conflicts = []

    def collapse(g):
        units = g["UNIDAD Corregida"].unique()
        if len(units) > 1:
            # BUGFIX (found in the national run): include_groups=False strips the
            # groupby key columns out of g, so the exported conflict rows were
            # missing Cuerpo.de.Agua/MATRIZ/Estación.POAL/Parámetro/fecha - only
            # a stringified _conflict_key survived. Reattach them explicitly.
            key_values = dict(zip(key, g.name if isinstance(g.name, tuple) else (g.name,)))
            conflict_rows = g.assign(**key_values)
            unit_conflicts.append(conflict_rows)

        lat_series = g["lat_final"].dropna()
        lon_series = g["lon_final"].dropna()

        return pd.Series({
            "valor": g["valor_num"].mean() if AGG_METHOD == "mean" else g["valor_num"].iloc[-1],
            "unidad": units[0] if len(units) == 1 else "CONFLICT-see poal_unit_conflicts.csv",
            "n_source_rows": len(g),
            "value_range": (g["valor_num"].max() - g["valor_num"].min()) if len(g) > 1 else 0.0,
            "toponimo": g["toponimo_canonico"].iloc[0],
            "toponimo_flag": g["toponimo_flag"].iloc[0],
            "lat": lat_series.iloc[0] if len(lat_series) else np.nan,
            "lon": lon_series.iloc[0] if len(lon_series) else np.nan,
            "coord_source": g["coord_source"].iloc[0],
            "año": g["AÑO"].iloc[0],
            "semestre": g["Semestre"].iloc[0],
            "laboratorio": g["Laboratorio"].iloc[0],
            "criterio": g["CRITERIO"].iloc[0],  # meaning unconfirmed, carried through as-is
        })

    clean = df.groupby(key, as_index=False, group_keys=False).apply(collapse, include_groups=False)
    clean = clean.reset_index(drop=True)

    n_collapsed = (clean["n_source_rows"] > 1).sum()
    print(f"  {len(df)} rows -> {len(clean)} rows after dedup "
          f"({n_collapsed} groups had multiple source rows collapsed)")

    unit_conflict_df = pd.concat(unit_conflicts, ignore_index=True) if unit_conflicts else pd.DataFrame()
    if len(unit_conflict_df):
        print(f"  WARNING: {len(unit_conflict_df)} rows involved in unit conflicts within a "
              f"dedup group - these were NOT averaged, see poal_unit_conflicts.csv")

    return clean, unit_conflict_df


# ──────────────────────────────────────────────────────────
# Supabase push (new)
# ──────────────────────────────────────────────────────────
def supabase_write(table, rows, on_conflict=None):
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
    try:
        supabase_write("sync_runs", [{
            "source": "poal",
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "status": status,
            "rows_written": rows_written,
            "error": error,
        }])
    except Exception as e:
        print(f"WARNING: could not write sync_runs log entry: {e}")


def rows_for_supabase(clean: pd.DataFrame):
    """Build the subset of rows actually pushed to the live database:
    only the bay + real comparison sites (SUPABASE_PUSH_BODIES), and
    only rows with a parseable date (the unique constraint on
    poal_readings needs a real fecha to upsert against)."""
    df = clean.copy()
    df = df[df["Cuerpo.de.Agua"].astype(str).str.upper().isin(SUPABASE_PUSH_BODIES)]
    df["fecha_str"] = pd.to_datetime(df["fecha_muestreo"], errors="coerce").dt.strftime("%Y-%m-%d")
    n_no_date = df["fecha_str"].isna().sum()
    if n_no_date:
        print(f"  {n_no_date} rows in the push scope have no parseable date and will be skipped "
              f"(can't upsert without one)")
    df = df[df["fecha_str"].notna()]

    def _clean_num(v):
        return None if pd.isna(v) else float(v)

    def _clean_int(v):
        return None if pd.isna(v) else int(v)

    def _clean_str(v):
        return None if pd.isna(v) else str(v)

    records = []
    for _, r in df.iterrows():
        records.append({
            "cuerpo_de_agua": _clean_str(r.get("Cuerpo.de.Agua")),
            "matriz": _clean_str(r.get("MATRIZ")),
            "estacion_poal": _clean_str(r.get("Estación.POAL")),
            "parametro": _clean_str(r.get("Parámetro")),
            "fecha": r["fecha_str"],
            "valor": _clean_num(r.get("valor")),
            "unidad": _clean_str(r.get("unidad")),
            "lat": _clean_num(r.get("lat")),
            "lon": _clean_num(r.get("lon")),
            "coord_source": _clean_str(r.get("coord_source")),
            "anio": _clean_int(r.get("año")),
            "semestre": _clean_str(r.get("semestre")),
            "laboratorio": _clean_str(r.get("laboratorio")),
            "n_source_rows": _clean_int(r.get("n_source_rows")),
        })
    return records


def push_to_supabase(clean: pd.DataFrame):
    records = rows_for_supabase(clean)
    print(f"  pushing {len(records)} rows (scope: {SUPABASE_PUSH_BODIES}) in "
          f"batches of {SUPABASE_BATCH_SIZE}")
    for i in range(0, len(records), SUPABASE_BATCH_SIZE):
        batch = records[i:i + SUPABASE_BATCH_SIZE]
        supabase_write(
            "poal_readings", batch,
            on_conflict="cuerpo_de_agua,estacion_poal,parametro,fecha"
        )
        print(f"    wrote rows {i}-{i + len(batch)}")
    return len(records)


def main():
    started_at = datetime.now(timezone.utc)
    rows_written = 0
    suffix = "national" if INCLUDE_ALL_BODIES else "quintero_bahia"

    try:
        print("=== 1. Download ===")
        zip_path = download_zip()

        print("\n=== 2. Extract ===")
        csv_path = extract_csv(zip_path)

        print("\n=== 3. Load ===")
        raw = load_raw(csv_path)

        print("\n=== 4. Filter to Bahia Quintero-Puchuncavi + comparison bodies ===")
        sub = filter_location(raw)

        print("\n=== 5. Clean types (numbers, dates, coordinates) ===")
        sub = clean_types(sub)

        print("\n=== 6. Flag station name inconsistencies ===")
        sub, name_variants = flag_station_name_variants(sub)

        print("\n=== 7. Resolve coordinates (recover from same-station history where corrupted) ===")
        sub, coord_issues = resolve_station_coordinates(sub)

        print("\n=== 8. Save rows that failed VALOR Corregida numeric parsing (not silently dropped) ===")
        bad_values = sub[sub["valor_num"].isna()][
            ["Cuerpo.de.Agua", "MATRIZ", "Estación.POAL", "Parámetro", "AÑO",
             "VALOR Corregida", "UNIDAD Corregida"]
        ].copy()
        if len(bad_values):
            bad_values.to_csv(f"poal_value_parse_failures_{suffix}.csv", index=False, encoding="utf-8-sig")
            print(f"  WARNING: {len(bad_values)} rows have a VALOR Corregida that doesn't parse as a "
                  f"number (e.g. '1.080.466.662' for a metals concentration - same digit-grouping "
                  f"corruption seen in coordinates). Saved to poal_value_parse_failures_{suffix}.csv, "
                  f"excluded from the clean dataset rather than guessed at.")

        print("\n=== 9. Dedupe (collapse to one row per station/parameter/date) ===")
        clean, unit_conflicts = dedupe(sub)

        print("\n=== 10. Save outputs (local files) ===")
        clean.to_csv(f"poal_clean_{suffix}.csv", index=False, encoding="utf-8-sig")
        print(f"  poal_clean_{suffix}.csv  ({len(clean)} rows)")

        name_variants.to_csv(f"poal_station_name_variants_{suffix}.csv", index=False, encoding="utf-8-sig")
        print(f"  poal_station_name_variants_{suffix}.csv  ({len(name_variants)} rows)")

        coord_issues.to_csv(f"poal_coordinate_issues_{suffix}.csv", index=False, encoding="utf-8-sig")
        print(f"  poal_coordinate_issues_{suffix}.csv  ({len(coord_issues)} rows)")

        if len(unit_conflicts):
            unit_conflicts.to_csv(f"poal_unit_conflicts_{suffix}.csv", index=False, encoding="utf-8-sig")
            print(f"  poal_unit_conflicts_{suffix}.csv  ({len(unit_conflicts)} rows)")

        print(f"\n=== 11. Push bay-scope rows to Supabase ===")
        rows_written = push_to_supabase(clean)

        log_sync_run(started_at, datetime.now(timezone.utc), "success", rows_written)
        print(f"\nDone. poal_clean_{suffix}.csv has every water body, matrix, and parameter DIRECTEMAR")
        print("publishes in POAL, saved locally. Only the bay + comparison sites were pushed live.")
        print("Review the name_variants and coordinate_issues files before trusting station identity/location.")

    except Exception as e:
        log_sync_run(started_at, datetime.now(timezone.utc), "failed", rows_written, error=str(e))
        print(f"\nBUILD FAILED: {e}")
        raise


if __name__ == "__main__":
    main()
