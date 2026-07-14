import re
import csv
import io
from datetime import datetime, date

import pandas as pd
import streamlit as st


# -----------------------------
# Helpers
# -----------------------------
MONEY_COLS = ["Amount", "Net amount", "Base amount", "Processing fee", "Payment amount", "Conversion rate"]
DATE_COLS = ["Created", "Updated"]

DEFAULT_FINAL_STATUSES = ["DONE", "FULLY_PAID"]
DEFAULT_PARTIAL_STATUSES = ["PARTIALLY_PAID"]

def sniff_delimiter(text: str) -> str:
    sample = text[:5000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=[",", ";", "\t", "|"])
        return dialect.delimiter
    except Exception:
        # fallback
        return ","

def parse_dt_series(s: pd.Series) -> pd.Series:
    # Obsługa dziwnych formatów typu: 12,07,2026 16:09:01
    def _parse_one(x):
        if pd.isna(x):
            return pd.NaT
        x = str(x).strip()
        if not x:
            return pd.NaT
        x = re.sub(r"^(\d{1,2}),(\d{1,2}),(\d{4})", r"\1.\2.\3", x)
        # dayfirst=True bo często PL/EU
        return pd.to_datetime(x, dayfirst=True, errors="coerce")
    return s.apply(_parse_one)

def to_numeric_series(s: pd.Series) -> pd.Series:
    # Usuwa spacje/nbsp, zamienia przecinek na kropkę
    return pd.to_numeric(
        s.astype(str)
         .str.replace("\u00a0", "", regex=False)
         .str.replace(" ", "", regex=False)
         .str.replace(",", ".", regex=False),
        errors="coerce"
    )

def ensure_columns(df: pd.DataFrame, cols: list[str]) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"Brak wymaganych kolumn w CSV: {missing}")

def dedup_by_payment_id_latest(df: pd.DataFrame, payment_col="Payment Id", updated_col="Updated_dt") -> pd.DataFrame:
    """
    Deduplikacja:
    - dla rekordów z Payment Id: zostawiamy najnowszy rekord wg Updated_dt
    - rekordy bez Payment Id zostają (traktujemy jak unikalne)
    """
    out = df.copy()

    # normalizacja Payment Id
    out[payment_col] = out[payment_col].astype("string")
    out.loc[out[payment_col].str.lower().isin(["nan", "none", ""], na=False), payment_col] = pd.NA

    with_pid = out[out[payment_col].notna()].sort_values(updated_col)
    latest = with_pid.groupby(payment_col).tail(1)

    no_pid = out[out[payment_col].isna()]
    return pd.concat([latest, no_pid], ignore_index=True)

def summarize_status_table(df: pd.DataFrame) -> pd.DataFrame:
    # bezpiecznie, jak nie ma którejś kolumny
    agg = {
        "rows": ("Uuid", "count") if "Uuid" in df.columns else (df.columns[0], "count"),
    }
    if "Trading Account" in df.columns:
        agg["uniq_accounts"] = ("Trading Account", "nunique")
    if "Amount_num" in df.columns:
        agg["sum_amount"] = ("Amount_num", "sum")
    if "Base amount_num" in df.columns:
        agg["sum_base"] = ("Base amount_num", "sum")

    tbl = (
        df.groupby("Status", dropna=False)
          .agg(**agg)
          .sort_values("sum_base" if "sum_base" in agg else "rows", ascending=False)
    )
    return tbl

def summarize_offer_table(df: pd.DataFrame) -> pd.DataFrame:
    if "Offer" not in df.columns:
        return pd.DataFrame()
    cols = []
    if "Amount_num" in df.columns: cols.append(("Amount_num", "sum"))
    if "Base amount_num" in df.columns: cols.append(("Base amount_num", "sum"))
    if not cols:
        return pd.DataFrame()
    agg = {f"sum_{c[0].replace(' ', '_').replace('_num','')}": c for c in cols}
    tbl = df.groupby("Offer").agg(**agg).sort_values(list(agg.keys())[-1], ascending=False)
    return tbl


# -----------------------------
# UI
# -----------------------------
st.set_page_config(page_title="Challenge Fees Calculator", layout="wide")
st.title("Challenge Fees Calculator (CSV)")

st.markdown(
    """
Wgraj eksport `deposits.csv` (lub podobny) i dostaniesz automatyczne podsumowania:
- **po statusach**
- warianty: **DONE+FULLY_PAID**, **DONE+FULLY_PAID+PARTIALLY_PAID**
- wersja z **deduplikacją po Payment Id**
"""
)

uploaded = st.file_uploader("Wgraj plik CSV", type=["csv"])
if not uploaded:
    st.stop()

raw_bytes = uploaded.read()
raw_text = raw_bytes.decode("utf-8", errors="ignore")
sep = sniff_delimiter(raw_text)

df = pd.read_csv(io.StringIO(raw_text), sep=sep)
df.columns = [c.strip() for c in df.columns]

# Walidacja minimalna
ensure_columns(df, ["Status"] + [c for c in DATE_COLS if c in df.columns])

# daty
for c in DATE_COLS:
    if c in df.columns:
        df[c + "_dt"] = parse_dt_series(df[c])

# kwoty
for c in MONEY_COLS:
    if c in df.columns:
        df[c + "_num"] = to_numeric_series(df[c])

# Sidebar: ustawienia
st.sidebar.header("Ustawienia")

date_basis = st.sidebar.selectbox(
    "Filtr dat po kolumnie",
    options=[c for c in ["Updated", "Created"] if c in df.columns],
    index=0
)
date_col = date_basis + "_dt"

min_dt = df[date_col].min()
max_dt = df[date_col].max()

# Domyślnie: od 1 czerwca bieżącego roku do dziś (o ile ma sens)
default_start = pd.Timestamp(date(max(datetime.now().year, 2026), 6, 1))  # możesz zmienić
default_end = pd.Timestamp(datetime.now())

start = st.sidebar.date_input("Start", value=(min_dt.date() if pd.notna(min_dt) else default_start.date()))
end = st.sidebar.date_input("Koniec", value=(max_dt.date() if pd.notna(max_dt) else default_end.date()))

start_ts = pd.Timestamp(start)
end_ts = pd.Timestamp(end) + pd.Timedelta(hours=23, minutes=59, seconds=59)

all_statuses = sorted(df["Status"].astype(str).unique().tolist())
final_statuses = st.sidebar.multiselect(
    "Statusy FINAL (zwykle liczone jako 'zebrane')",
    options=all_statuses,
    default=[s for s in DEFAULT_FINAL_STATUSES if s in all_statuses]
)
partial_statuses = st.sidebar.multiselect(
    "Statusy PARTIAL (opcjonalnie też liczone)",
    options=all_statuses,
    default=[s for s in DEFAULT_PARTIAL_STATUSES if s in all_statuses]
)

do_dedup = st.sidebar.checkbox("Deduplikuj po Payment Id (zostaw najnowszy wpis)", value=True)

# Filtr zakresu dat
mask_range = df[date_col].between(start_ts, end_ts)
df_range = df[mask_range].copy()

st.subheader("1) Podstawowe info o pliku")
c1, c2, c3 = st.columns(3)
c1.metric("Wiersze w pliku", f"{len(df):,}".replace(",", " "))
c2.metric(f"Wiersze w zakresie ({date_basis})", f"{len(df_range):,}".replace(",", " "))
if "Base currency" in df.columns:
    c3.metric("Base currency (unikalne)", str(df["Base currency"].nunique(dropna=True)))

st.subheader("2) Tabela statusów (w zakresie dat)")
status_tbl = summarize_status_table(df_range)
st.dataframe(status_tbl, use_container_width=True)

st.subheader("3) Podsumowania (warianty liczenia)")
def calc_sum(sub: pd.DataFrame) -> tuple[float, float, int]:
    sum_base = float(sub["Base amount_num"].sum()) if "Base amount_num" in sub.columns else float("nan")
    sum_amount = float(sub["Amount_num"].sum()) if "Amount_num" in sub.columns else float("nan")
    return sum_base, sum_amount, len(sub)

# warianty
variant_a = df_range[df_range["Status"].isin(final_statuses)]
variant_b = df_range[df_range["Status"].isin(final_statuses + partial_statuses)]

a_base, a_amt, a_rows = calc_sum(variant_a)
b_base, b_amt, b_rows = calc_sum(variant_b)

colA, colB = st.columns(2)
with colA:
    st.markdown("**Wariant A: FINAL**")
    st.write(f"Statusy: `{final_statuses}`")
    st.metric("Suma Base amount", f"{a_base:,.2f}".replace(",", " "))
    st.metric("Suma Amount", f"{a_amt:,.2f}".replace(",", " "))
    st.metric("Wiersze", f"{a_rows:,}".replace(",", " "))

with colB:
    st.markdown("**Wariant B: FINAL + PARTIAL**")
    st.write(f"Statusy: `{final_statuses + partial_statuses}`")
    st.metric("Suma Base amount", f"{b_base:,.2f}".replace(",", " "))
    st.metric("Suma Amount", f"{b_amt:,.2f}".replace(",", " "))
    st.metric("Wiersze", f"{b_rows:,}".replace(",", " "))

if do_dedup and "Payment Id" in df_range.columns and date_col in df_range.columns:
    st.subheader("4) Deduplikacja po Payment Id (latest by date)")
    dedup_input = variant_b.copy()
    # potrzebne do dedup: Updated_dt; jeśli user filtruje po Created, nadal dedup robimy po Updated_dt, jeśli jest
    if "Updated_dt" not in dedup_input.columns:
        st.warning("Brak kolumny Updated/Updated_dt – nie mogę deduplikować po 'najświeższym Updated'.")
    else:
        deduped = dedup_by_payment_id_latest(dedup_input, payment_col="Payment Id", updated_col="Updated_dt")
        d_base, d_amt, d_rows = calc_sum(deduped)

        c1, c2, c3 = st.columns(3)
        c1.metric("Wiersze przed dedup", f"{len(dedup_input):,}".replace(",", " "))
        c2.metric("Wiersze po dedup", f"{d_rows:,}".replace(",", " "))
        c3.metric("Suma Base amount (dedup)", f"{d_base:,.2f}".replace(",", " "))

        st.caption("Dedup usuwa wielokrotne wpisy tego samego Payment Id (zostaje najnowszy Updated).")

st.subheader("5) Top Offer (opcjonalnie)")
offer_tbl = summarize_offer_table(df_range)
if offer_tbl.empty:
    st.info("Brak kolumny `Offer` albo brak kolumn kwotowych do zsumowania.")
else:
    st.dataframe(offer_tbl.head(30), use_container_width=True)

st.subheader("6) Eksport do CSV (wyniki)")
# eksport status_tbl
status_csv = status_tbl.reset_index().to_csv(index=False).encode("utf-8")
st.download_button("Pobierz: status_summary.csv", data=status_csv, file_name="status_summary.csv", mime="text/csv")

if not offer_tbl.empty:
    offer_csv = offer_tbl.reset_index().to_csv(index=False).encode("utf-8")
    st.download_button("Pobierz: offer_summary.csv", data=offer_csv, file_name="offer_summary.csv", mime="text/csv")
