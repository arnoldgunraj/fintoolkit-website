#!/usr/bin/env python3
"""
Bank CSV to NetSuite Bank Statement Import CSV Converter — Dynamic Engine

Instead of hardcoded per-bank parsers, this script uses a dynamic column-matching
engine that:
  1. Reads header names from any CSV
  2. Scores each column against known keyword patterns for each NetSuite field
     (date, amount, credit, debit, description, payee, reference, type, etc.)
  3. Infers column roles from sample data when headers are ambiguous
  4. Automatically handles single-amount vs split credit/debit layouts
  5. Filters balance/summary rows using keyword + BAI-code detection

This means NEW bank formats are handled automatically — no code changes needed.

Output columns:
    Date (MM/DD/YYYY), Payer/Payee Name, Transaction Id, Transaction Type,
    Amount, Memo, NS Internal Customer Id, NS Customer Name, Invoice Number(s)

Usage:
    python convert_bank_csv.py --input-dir /path/to/csvs --output-dir /path/to/output
"""

import argparse
import csv
import json
import os
import re
import sys
import threading
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
# ═══════════════════════════════════════════════════════════════════════════
# REMOTE CONFIG LOADER
# ═══════════════════════════════════════════════════════════════════════════
# Fetches converter_config.json from GitHub on startup.
# Caches locally so it works offline. Falls back to hardcoded defaults
# if both remote fetch and cache fail.
#
# To push a bank format update: edit converter_config.json in the
# fintoolkit-website GitHub repo and increment _version. All installed
# copies will pick it up automatically on next launch.

CONFIG_URL = (
    "https://raw.githubusercontent.com/arnoldgunraj/"
    "fintoolkit-website/main/converter_config.json"
)

# Local cache path — same directory as the exe
_SCRIPT_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
_CACHE_PATH = Path(os.path.expanduser("~")) / ".fintoolkit_converter_config.json"


def _fetch_remote_config(timeout: int = 8) -> dict | None:
    """Fetch config from GitHub. Returns dict or None on failure."""
    try:
        req = urllib.request.Request(
            CONFIG_URL,
            headers={"User-Agent": "FintoolKit-BankBridge/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data
    except Exception:
        return None


def _load_cached_config() -> dict | None:
    """Load config from local cache file."""
    try:
        if _CACHE_PATH.exists():
            with open(_CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _save_config_cache(cfg: dict):
    """Save config to local cache."""
    try:
        with open(_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


def load_converter_config() -> dict:
    """
    Load converter config with update check.
    Priority: remote (if newer) > local cache > hardcoded defaults.
    Non-blocking: remote fetch happens with timeout, falls back silently.
    """
    cached = _load_cached_config()
    cached_version = cached.get("_version", 0) if cached else 0

    remote = _fetch_remote_config()
    if remote:
        remote_version = remote.get("_version", 0)
        if remote_version > cached_version:
            _save_config_cache(remote)
            return remote
        return cached if cached else remote

    if cached:
        return cached

    # Final fallback: hardcoded defaults (always works offline)
    return _get_default_config()


def _get_default_config() -> dict:
    """Hardcoded fallback config — used only when remote and cache both fail."""
    return {
        "_version": 0,
        "date_formats": [
            "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%B %d, %Y",
            "%b %d, %Y", "%d-%b-%Y", "%d/%m/%Y", "%Y/%m/%d",
            "%m-%d-%Y", "%m-%d-%y",
        ],
        "balance_keywords": [
            "OPENING LEDGER BALANCE", "CLOSING LEDGER BALANCE",
            "OPENING AVAILABLE BALANCE", "CLOSING AVAILABLE BALANCE",
            "NO_INFORMATION_AVAILABLE", "BEGINNING BALANCE", "ENDING BALANCE",
            "LEDGER BALANCE", "AVAILABLE BALANCE", "TOTAL CREDITS",
            "TOTAL DEBITS", "TOTAL CREDIT", "TOTAL DEBIT",
        ],
        "failed_status_values": [
            "FAILED", "REJECTED", "CANCELLED", "CANCELED", "REVERSED", "DECLINED"
        ],
        "role_header_keywords": {
            "date": [["AS OF DATE", 10], ["DATE", 6]],
            "amount": [["TRANSACTION AMOUNT", 10], ["AMOUNT", 8]],
            "credit_amount": [["CREDIT AMOUNT", 10], ["CREDIT", 6]],
            "debit_amount": [["DEBIT AMOUNT", 10], ["DEBIT", 6]],
            "description": [["DESCRIPTION", 8], ["MEMO", 6], ["NAME", 4]],
            "description2": [["MEMO", 6], ["NOTES", 5]],
            "reference": [["TRANSACTION ID", 10], ["REFERENCE", 6]],
            "tran_type_raw": [["TRANSACTION TYPE", 10], ["TYPE", 4]],
            "bai_code": [["BAI TYPE CODE", 10], ["BAI CODE", 10]],
            "credit_debit_indicator": [["CREDIT OR DEBIT", 10]],
            "record_type": [["RECORD TYPE", 10]],
        },
        "type_keyword_rules": [
            [["CHECK PAID", "CHECK NUMBER"], "Check"],
            [["WIRE TRANSFER", "WIRE IN", "WIRE OUT"], "Transfer"],
            [["TRANSFER", "SWEEP"], "Transfer"],
            [["SERVICE CHARGE"], "Payment"],
            [["ACH CREDIT"], "Deposit"],
            [["ACH DEBIT"], "Payment"],
            [["CREDIT", "DEPOSIT"], "Deposit"],
            [["DEBIT", "WITHDRAWAL", "PAYMENT"], "Payment"],
        ],
    }


# Load config once at module import time
_CONFIG = load_converter_config()



# ═══════════════════════════════════════════════════════════════════════════
# DATE PARSING
# ═══════════════════════════════════════════════════════════════════════════

def _get_date_formats():
    return _CONFIG.get("date_formats", [
        "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%B %d, %Y",
        "%b %d, %Y", "%d-%b-%Y", "%d/%m/%Y", "%Y/%m/%d",
        "%m-%d-%Y", "%m-%d-%y",
    ])


def parse_date(raw: str) -> str:
    """Return date as MM/DD/YYYY, or '' on failure."""
    raw = raw.strip().strip('"')
    if not raw:
        return ""
    for fmt in _get_date_formats():
        try:
            return datetime.strptime(raw, fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue
    # Non-zero-padded fallback  (e.g. "2/3/2026")
    parts = raw.split("/")
    if len(parts) == 3:
        try:
            m, d, y = parts
            if len(y) == 2:
                y = "20" + y
            return datetime(int(y), int(m), int(d)).strftime("%m/%d/%Y")
        except (ValueError, IndexError):
            pass
    return ""


def looks_like_date(value: str) -> bool:
    """Heuristic: does this string look like a date?"""
    return parse_date(value) != ""


# ═══════════════════════════════════════════════════════════════════════════
# AMOUNT PARSING
# ═══════════════════════════════════════════════════════════════════════════

def parse_amount(raw) -> float:
    if raw is None:
        return 0.0
    raw = str(raw).strip().strip('"')
    if not raw:
        return 0.0
    cleaned = raw.replace("$", "").replace(",", "").strip()
    # Handle parenthetical negatives like (1234.56)
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def looks_like_amount(value: str) -> bool:
    """Heuristic: does this string look like a monetary amount?"""
    cleaned = value.strip().strip('"').replace("$", "").replace(",", "").strip()
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = cleaned[1:-1]
    try:
        float(cleaned)
        return bool(cleaned) and cleaned != "0"
    except ValueError:
        return False


# ═══════════════════════════════════════════════════════════════════════════
# BALANCE / SUMMARY ROW FILTERING
# ═══════════════════════════════════════════════════════════════════════════

def _get_balance_keywords():
    return _CONFIG.get("balance_keywords", [
        "OPENING LEDGER BALANCE", "CLOSING LEDGER BALANCE",
        "BEGINNING BALANCE", "ENDING BALANCE", "TOTAL CREDITS", "TOTAL DEBITS",
    ])

# BAI codes that represent balance summaries / totals (not transactions)
BALANCE_BAI_CODES = {
    "10", "15", "40", "45",        # Opening/Closing balances
    "43", "50", "55",              # Average balances
    "63", "72", "73", "74", "75",  # Float / adjustment summaries
    "100", "101", "102",           # Total credits / credit count
    "140",                         # Total ACH credits
    "400", "401", "402",           # Total debits / debit count
    "420",                         # Total ACH debits
    "450",                         # Total ACH related debits
    "470",                         # Total check paid
    "505", "515",                  # Total automatic transfer / controlled disburse
    "640",                         # Total return item amount
    "650",                         # Total loan payment
}


def is_balance_row(text: str, bai_code: str = "") -> bool:
    """Return True if the row is a balance summary, not an actual transaction."""
    upper = (text or "").upper()
    for kw in _get_balance_keywords():
        if kw in upper:
            return True
    # Catch-all: any description starting with "TOTAL " is a summary row
    if upper.startswith("TOTAL "):
        return True
    # Only filter on BAI code when an actual code is present (not empty)
    if bai_code and bai_code in BALANCE_BAI_CODES:
        return True
    return False


# ═══════════════════════════════════════════════════════════════════════════
# TRANSACTION TYPE MAPPING
# ═══════════════════════════════════════════════════════════════════════════

BAI_TYPE_MAP = {
    # Credits / Deposits
    "108": "Deposit", "142": "Deposit", "165": "Deposit", "169": "Deposit",
    "195": "Deposit", "199": "Deposit", "275": "Deposit", "295": "Deposit",
    "399": "Deposit",
    # Transfers
    "206": "Transfer", "575": "Transfer", "506": "Transfer",
    # Debits / Payments
    "409": "Payment", "451": "Payment", "469": "Payment",
    "495": "Payment", "501": "Payment", "555": "Payment",
    "698": "Payment", "699": "Payment",
    # Check
    "475": "Check",
}

# Standard BAI descriptions — used for the Payee Name field when the bank's
# DESCRIPTION column is too generic (e.g. "EFT CREDIT", "EFT DEBIT").
BAI_DESCRIPTION_MAP = {
    # Credits
    "108": "Credit Reversal",
    "115": "Lockbox Deposit",
    "118": "Lockbox Adjustment",
    "131": "Individual Loan Deposit",
    "135": "Total Loan Deposits",
    "140": "Total ACH Credits",
    "142": "ACH Credit Received",
    "145": "ACH Concentration Credit",
    "147": "Individual ACH Return",
    "155": "Preauthorized ACH Credit Reject",
    "160": "Item in Process of Collection",
    "164": "Corporate Trade Payment Credit",
    "165": "Preauthorized ACH Credit",
    "166": "ACH Settlement",
    "169": "ACH Return Item or Adjustment",
    "171": "Individual Incoming Internal Money Transfer",
    "175": "Incoming Money Transfer",
    "183": "Bond Operations Credit",
    "185": "Miscellaneous Credit",
    "187": "Cash Letter Credit",
    "190": "Deposit Correction",
    "191": "Bank-Originated Credit",
    "195": "Check Deposit",
    "198": "Miscellaneous ACH Credit",
    "199": "Miscellaneous Deposit",
    "201": "Individual Loan Payment",
    "202": "Consolidation of Loans",
    "206": "Transfer of Funds",
    "208": "Trust Credit",
    "212": "Controlled Disbursement Credit",
    "230": "Incoming International Money Transfer",
    "232": "Foreign Letter of Credit",
    "255": "Credit Adjustment",
    "261": "Fed Funds Purchased",
    "263": "Transfer Credit",
    "266": "International Credit Adjustment",
    "268": "Foreign Exchange of Credit",
    "275": "Wire Transfer Credit",
    "277": "Fed Funds Sold",
    "279": "Rehypothecation Credit",
    "281": "Domestic Collection Credit",
    "283": "Cash Concentration Transfer Credit",
    "285": "Miscellaneous Wire Transfer",
    "295": "Checks Deposited",
    "301": "Commercial Paper Deposit",
    "306": "Treasury Tax Deposit",
    "354": "YTD Credit Adjustment",
    "399": "Miscellaneous Credit",
    # Debits
    "401": "Total Debits",
    "409": "Debit Reversal",
    "415": "Lockbox Debit",
    "420": "Total ACH Debits",
    "421": "ACH Debit Received",
    "422": "ACH Return",
    "423": "ACH Concentration Debit",
    "435": "Total Loan Payments",
    "445": "ACH Debit Reject",
    "447": "Individual ACH Debit Return",
    "451": "Preauthorized ACH Debit",
    "452": "ACH Debit - Preauthorized",
    "455": "Preauthorized ACH Debit Reject",
    "462": "Payable-Through Draft",
    "466": "ACH Settlement Charge",
    "469": "ACH Debit Return or Adjustment",
    "470": "Total Check Paid",
    "471": "Individual Check Paid",
    "472": "Certified Check",
    "474": "Check Paid - Cummulative Total",
    "475": "Check Paid",
    "476": "Check Paid - Reversal",
    "477": "Domestic Collection Debit",
    "481": "Individual Outgoing Internal Money Transfer",
    "487": "Cash Letter Debit",
    "489": "Cash Letter Adjustment",
    "495": "Check Paid Total",
    "496": "Draft",
    "498": "Miscellaneous ACH Debit",
    "499": "Miscellaneous Debit",
    "501": "Individual Automatic Transfer Debit",
    "502": "Bond Operations Debit",
    "505": "Total Automatic Transfer Debits",
    "506": "Transfer of Funds - Debit",
    "507": "Manual Transfer Debit",
    "512": "Controlled Disbursement Debit",
    "515": "Total Controlled Disburse Debits",
    "516": "Controlled Disburse Debit - Funded",
    "530": "Outgoing International Money Transfer",
    "535": "International Money Transfer Debit",
    "555": "Wire Transfer Debit",
    "563": "Transfer Debit",
    "564": "International Debit Adjustment",
    "566": "Foreign Exchange Debit",
    "575": "Wire Transfer",
    "577": "Fed Funds Purchased",
    "616": "Federal Reserve DTC Credit",
    "617": "Federal Reserve DTC Debit",
    "621": "Return Item Adjustment",
    "623": "Customer Terminal Activity",
    "625": "ACH Reversal",
    "627": "Payable-Through Draft Debit",
    "631": "Return Item",
    "633": "Return Item Adjustment",
    "640": "Total Return Item Amount",
    "646": "Return Item - Unposted",
    "650": "Total Loan Payment",
    "661": "Individual Escrow Payment",
    "672": "Cumulative Checks Paid",
    "674": "Certified Check Debit",
    "676": "Check Paid - Reversal",
    "681": "Individual Cash Letter Debit",
    "686": "Cash Letter Debit Adjustment",
    "690": "Outgoing Internal Money Transfer",
    "693": "Interest Debit",
    "695": "Overdraft Interest",
    "698": "Service Charge",
    "699": "Miscellaneous Fee",
}

def _get_type_keyword_rules():
    cfg = _CONFIG.get("type_keyword_rules", [])
    if cfg:
        return [(tuple(kws), ns_type) for kws, ns_type in cfg]
    return TYPE_KEYWORD_RULES_DEFAULT


TYPE_KEYWORD_RULES_DEFAULT = [

    # (keywords_in_combined_text, netsuite_type)  — checked in order
    (["CHECK PAID", "CHECK NUMBER"], "Check"),
    (["WIRE TRANSFER", "WIRE IN", "WIRE OUT"], "Transfer"),
    (["TRANSFER", "SWEEP", "ZERO BAL TRF", "BOOK TRANSFER"], "Transfer"),
    (["SERVICE CHARGE", "ANALYSIS SERVICE"], "Payment"),
    (["ACH CREDIT", "ACH_CREDIT", "PREAUTHORIZED ACH CREDIT"], "Deposit"),
    (["ACH DEBIT", "ACH_DEBIT", "PREAUTHORIZED ACH DEBIT"], "Payment"),
    (["CREDIT", "DEPOSIT"], "Deposit"),
    (["DEBIT", "WITHDRAWAL", "PAYMENT"], "Payment"),
]


def map_transaction_type(description: str, amount: float,
                         bai_code: str = "", raw_tran_type: str = "") -> str:
    """Derive a NetSuite transaction type from available source fields."""
    # 1. BAI code lookup
    if bai_code and bai_code in BAI_TYPE_MAP:
        return BAI_TYPE_MAP[bai_code]

    # 2. Keyword scan across description + raw type
    combined = f"{(description or '').upper()} {(raw_tran_type or '').upper()}"
    for keywords, ns_type in _get_type_keyword_rules():
        if any(kw in combined for kw in keywords):
            return ns_type

    # 3. Sign of amount
    if amount > 0:
        return "Deposit"
    elif amount < 0:
        return "Payment"
    return ""


# ═══════════════════════════════════════════════════════════════════════════
# PAYEE EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════

_PAYEE_STRIP_PREFIXES = [
    "ELECTRONIC DEPOSIT ", "Direct Deposit ", "ACH Paymen ",
    "Withdrawal ACH ", "Deposit ACH ", "ACH Credit Receipt",
    "ACH Debit ", "Preauthorized ACH Credit", "Preauthorized ACH Debit",
]


def extract_payee(text: str) -> str:
    """Pull a clean payee name from a description string."""
    if not text:
        return ""
    cleaned = text.strip()
    for pfx in _PAYEE_STRIP_PREFIXES:
        if cleaned.upper().startswith(pfx.upper()):
            cleaned = cleaned[len(pfx):].strip()
            break
    payee = re.split(r"\s{3,}", cleaned)[0].strip()
    return payee[:80]


# ═══════════════════════════════════════════════════════════════════════════
# DYNAMIC COLUMN MAPPER  — the heart of the engine
# ═══════════════════════════════════════════════════════════════════════════

# Each "role" is a NetSuite-relevant concept.  The mapper scores every
# column header against these keyword lists, picks the best match, then
# confirms with data-type inference when needed.

def _get_role_keywords():
    cfg = _CONFIG.get("role_header_keywords", {})
    # Convert list-of-lists back to list-of-tuples for compatibility
    return {
        role: [(kw, wt) for kw, wt in pairs]
        for role, pairs in cfg.items()
    } if cfg else ROLE_HEADER_KEYWORDS_DEFAULT


ROLE_HEADER_KEYWORDS_DEFAULT = {

    # --- Date ---
    "date": [
        ("AS OF DATE", 10), ("TRADE DATE", 10), ("SETTLEMENT DATE", 8),
        ("POST DATE", 10), ("POSTING DATE", 10), ("PROCESSED DATE", 10),
        ("POST", 5), ("DATE", 6),
    ],
    # --- Amount (single column, can be +/-) ---
    "amount": [
        ("TRANSACTION AMOUNT", 10), ("AMOUNT", 8),
    ],
    # --- Credit amount (positive / deposit) ---
    "credit_amount": [
        ("CREDIT AMOUNT", 10), ("CREDIT", 6),
    ],
    # --- Debit amount (positive value representing outflow) ---
    "debit_amount": [
        ("DEBIT AMOUNT", 10), ("DEBIT", 6),
    ],
    # --- Description / Memo ---
    "description": [
        ("DESCRIPTION", 8), ("TRANSACTION DESCRIPTION", 10),
        ("DETAIL", 7), ("MEMO", 6), ("NAME", 4),
        ("REMARK 01", 5), ("REASON FOR PAYMENT", 5),
    ],
    # --- Secondary description (used as memo if primary exists) ---
    "description2": [
        ("DETAIL", 7), ("MEMO", 6), ("NOTES", 5),
        ("REASON FOR PAYMENT", 5), ("REMARK 01", 4), ("NAME", 3),
    ],
    # --- Transaction / bank reference ID ---
    "reference": [
        ("TRANSACTION ID", 10), ("BANK REF #", 9), ("BANK REF", 9),
        ("BANK REFERENCE", 9), ("CUSTOMER REF #", 8), ("CUSTOMER REF", 8),
        ("REFERENCE", 6), ("CHECK NUMBER", 8), ("CHECK", 5),
        ("CHECK OR SLIP #", 8), ("END TO END ID", 6),
    ],
    # --- Raw transaction type from bank ---
    "tran_type_raw": [
        ("TRAN TYPE", 10), ("TRANSACTION TYPE", 10), ("BAI DESCRIPTION", 9),
        ("TYPE", 4), ("TRANSACTION", 4),
    ],
    # --- BAI code ---
    "bai_code": [
        ("BAI TYPE CODE", 10), ("BAI CODE", 10), ("CODE", 4),
    ],
    # --- Credit-or-Debit indicator column ---
    "credit_debit_indicator": [
        ("CREDIT OR DEBIT", 10), ("DEBIT/CREDIT", 10),
        ("DEBIT CREDIT", 8),
    ],
    # --- Record type (for filtering non-detail rows) ---
    "record_type": [
        ("RECORD TYPE", 10), ("DETAILS", 8),
    ],
}

# Roles that should NOT overlap with the same column.
# Each group means: at most one role per column within the group.
EXCLUSIVE_ROLE_GROUPS = [
    {"credit_amount", "debit_amount", "amount"},
    {"description", "description2"},
    {"credit_amount", "debit_amount", "credit_debit_indicator"},
    {"description", "record_type"},
]


class ColumnMapping:
    """Result of the dynamic column mapper."""

    def __init__(self):
        self.date_col: int | None = None
        self.amount_col: int | None = None         # single-amount layout
        self.credit_col: int | None = None          # split layout
        self.debit_col: int | None = None           # split layout
        self.description_col: int | None = None
        self.description2_col: int | None = None    # secondary desc / memo
        self.reference_col: int | None = None
        self.tran_type_col: int | None = None
        self.bai_code_col: int | None = None
        self.cd_indicator_col: int | None = None    # "Credit or Debit" text
        self.record_type_col: int | None = None
        self.status_col: int | None = None          # Mercury: Status column
        self.remark_cols: list[int] = []            # REMARK 01–15 col indices

    @property
    def is_split_amount(self) -> bool:
        return self.credit_col is not None and self.debit_col is not None

    def __repr__(self):
        parts = []
        for attr in ("date_col", "amount_col", "credit_col", "debit_col",
                      "description_col", "description2_col", "reference_col",
                      "tran_type_col", "bai_code_col", "cd_indicator_col",
                      "record_type_col"):
            val = getattr(self, attr)
            if val is not None:
                parts.append(f"{attr}={val}")
        if self.remark_cols:
            parts.append(f"remark_cols={self.remark_cols}")
        return f"ColumnMapping({', '.join(parts)})"


def _normalize(header: str) -> str:
    return header.strip().strip('"').upper()


def map_columns(headers: list, sample_rows: list) -> ColumnMapping:
    """
    Score every header against every role's keyword list.
    Resolve conflicts (e.g. two columns both match "CREDIT").
    Fall back to data-type inference when headers are ambiguous.
    """
    n = len(headers)
    norm = [_normalize(h) for h in headers]
    mapping = ColumnMapping()

    # --- Phase 1: Score each (column, role) pair ----
    # scores[role] = [(col_idx, score), ...]
    scores: dict[str, list[tuple[int, int]]] = {role: [] for role in _get_role_keywords()}

    for role, keywords in _get_role_keywords().items():
        for col_idx, h in enumerate(norm):
            if not h:
                continue
            best_score = 0
            for kw, weight in keywords:
                if kw == h:
                    # Exact match — strongest signal
                    best_score = max(best_score, weight + 5)
                elif kw in h:
                    # Keyword is a substring of the header
                    # e.g. "CREDIT" found inside "CREDIT AMOUNT" ✓
                    best_score = max(best_score, weight)
                # NOTE: We intentionally do NOT check (h in kw) — the reverse
                # substring match is too loose.  e.g. "AMOUNT" in "CREDIT AMOUNT"
                # would wrongly assign "credit_amount" to a plain "AMOUNT" column,
                # and "DETAIL" in "DETAILS" would wrongly flag a description col
                # as a record_type column.
            if best_score > 0:
                scores[role].append((col_idx, best_score))

    # Sort each role's candidates by score descending
    for role in scores:
        scores[role].sort(key=lambda x: -x[1])

    # --- Phase 2: Assign roles greedily, respecting exclusivity ---
    assigned_cols: dict[str, int] = {}   # role -> col_idx
    used_cols: set[int] = set()

    # Process roles in priority order.
    # We try credit_amount/debit_amount BEFORE amount so that when both
    # "Credit" and "Debit" columns exist, the split layout wins.
    # But the exclusivity groups prevent "amount" from colliding.
    role_priority = [
        "date", "bai_code", "credit_debit_indicator", "record_type",
        "credit_amount", "debit_amount", "amount",
        "tran_type_raw", "reference",
        "description", "description2",
    ]

    for role in role_priority:
        for col_idx, score in scores.get(role, []):
            # Check exclusivity: don't assign the same col to conflicting roles
            conflict = False
            for group in EXCLUSIVE_ROLE_GROUPS:
                if role in group:
                    for other_role in group:
                        if other_role != role and other_role in assigned_cols:
                            if assigned_cols[other_role] == col_idx:
                                conflict = True
                                break
            if conflict:
                continue

            # For description2, don't reuse the same col as description
            if role == "description2" and col_idx == assigned_cols.get("description"):
                continue

            assigned_cols[role] = col_idx
            used_cols.add(col_idx)
            break

    # --- Phase 3: Data-type inference for missing critical roles ---
    # If we still don't have a date column, scan data to find one
    if "date" not in assigned_cols:
        for col_idx in range(n):
            if col_idx in used_cols:
                continue
            date_hits = sum(1 for row in sample_rows[:10]
                           if col_idx < len(row) and looks_like_date(row[col_idx]))
            if date_hits >= min(3, len(sample_rows[:10]) * 0.5):
                assigned_cols["date"] = col_idx
                used_cols.add(col_idx)
                break

    # If we have neither amount nor credit/debit, scan for numeric columns
    has_amount = "amount" in assigned_cols
    has_split = "credit_amount" in assigned_cols and "debit_amount" in assigned_cols

    if not has_amount and not has_split:
        numeric_cols = []
        for col_idx in range(n):
            if col_idx in used_cols:
                continue
            hits = 0
            is_monetary = False
            for row in sample_rows[:10]:
                if col_idx < len(row):
                    val = row[col_idx].strip().strip('"')
                    if looks_like_amount(val):
                        hits += 1
                        # Monetary amounts typically have decimals, $, commas,
                        # or are reasonably short.  Skip columns that look like
                        # long ID numbers (e.g. "000000804799655").
                        if "." in val or "$" in val or "," in val or len(val) < 10:
                            is_monetary = True
            if hits >= 2 and is_monetary:
                numeric_cols.append((col_idx, hits))
        numeric_cols.sort(key=lambda x: -x[1])

        if len(numeric_cols) >= 2:
            # Likely split credit/debit
            assigned_cols["credit_amount"] = numeric_cols[0][0]
            assigned_cols["debit_amount"] = numeric_cols[1][0]
        elif len(numeric_cols) == 1:
            assigned_cols["amount"] = numeric_cols[0][0]

    # If no description found, pick the longest text column not yet used
    if "description" not in assigned_cols:
        best_col = None
        best_avg_len = 0
        for col_idx in range(n):
            if col_idx in used_cols:
                continue
            avg_len = 0
            count = 0
            for row in sample_rows[:10]:
                if col_idx < len(row):
                    val = row[col_idx].strip().strip('"')
                    if val and not looks_like_amount(val) and not looks_like_date(val):
                        avg_len += len(val)
                        count += 1
            if count > 0:
                avg_len /= count
                if avg_len > best_avg_len:
                    best_avg_len = avg_len
                    best_col = col_idx
        if best_col is not None and best_avg_len > 5:
            assigned_cols["description"] = best_col

    # --- Phase 4: Populate the ColumnMapping object ---
    mapping.date_col = assigned_cols.get("date")
    mapping.amount_col = assigned_cols.get("amount")
    mapping.credit_col = assigned_cols.get("credit_amount")
    mapping.debit_col = assigned_cols.get("debit_amount")
    mapping.description_col = assigned_cols.get("description")
    mapping.description2_col = assigned_cols.get("description2")
    mapping.reference_col = assigned_cols.get("reference")
    mapping.tran_type_col = assigned_cols.get("tran_type_raw")
    mapping.bai_code_col = assigned_cols.get("bai_code")
    mapping.cd_indicator_col = assigned_cols.get("credit_debit_indicator")
    mapping.record_type_col = assigned_cols.get("record_type")

    # --- Phase 5: Detect REMARK columns (REMARK 01 through REMARK 15) ---
    # These are common in BAI2 / JPMorgan bank exports and contain the full
    # transaction detail that should be concatenated into the Memo field.
    remark_cols = []
    for col_idx, h in enumerate(norm):
        if re.match(r"^REMARK\s*\d{1,2}$", h):
            remark_cols.append(col_idx)
    # Sort by column index to preserve order
    remark_cols.sort()
    mapping.remark_cols = remark_cols

    # --- Phase 6: Detect Mercury Status column ---
    for col_idx, h in enumerate(norm):
        if h == "STATUS":
            mapping.status_col = col_idx
            break

    return mapping


# ═══════════════════════════════════════════════════════════════════════════
# UNIVERSAL ROW PARSER  — works with any ColumnMapping
# ═══════════════════════════════════════════════════════════════════════════

NETSUITE_HEADERS = [
    "Date (MM/DD/YYYY)", "Payer/Payee Name", "Transaction Id",
    "Transaction Type", "Amount", "Memo",
    "NS Internal Customer Id", "NS Customer Name", "Invoice Number(s)",
]


def _col(row, idx):
    """Safe column access, returns stripped string."""
    if idx is None or idx >= len(row):
        return ""
    return row[idx].strip().strip('"')


def _build_remark_memo(row, remark_cols):
    """
    Concatenate all non-empty REMARK columns into a single memo string.
    Strips the 'ORIG CO NAME=' prefix from the value (keeps the rest).
    """
    parts = []
    for col_idx in remark_cols:
        val = _col(row, col_idx)
        if val:
            parts.append(val)
    if not parts:
        return ""

    # Clean up key=value pairs: strip "ORIG CO NAME=" prefix
    cleaned = []
    for part in parts:
        if part.upper().startswith("ORIG CO NAME="):
            # Drop the prefix, keep the value after "="
            cleaned.append(part.split("=", 1)[1])
        else:
            cleaned.append(part)

    return ";".join(cleaned)


def parse_rows(mapping: ColumnMapping, data_rows: list) -> list:
    """
    Convert raw CSV rows into NetSuite-ready dicts using the dynamic mapping.
    Returns a list of [date, payee, tran_id, tran_type, amount, memo, '', '', ''].
    """
    results = []

    for row in data_rows:
        # --- Record type filter (e.g. Berkshire "Detail" vs "Summary") ---
        if mapping.record_type_col is not None:
            rt = _col(row, mapping.record_type_col).upper()
            # Skip non-detail rows when record type column exists
            if rt and rt not in ("DETAIL", "DEBIT", "CREDIT", ""):
                continue

        # --- Skip failed/rejected transactions (Mercury and others) ---
        if mapping.status_col is not None:
            status_val = _col(row, mapping.status_col).upper()
            failed_vals = [v.upper() for v in _CONFIG.get(
                "failed_status_values",
                ["FAILED", "REJECTED", "CANCELLED", "CANCELED", "REVERSED", "DECLINED"]
            )]
            if status_val in failed_vals:
                continue

        # --- Date ---
        date_str = parse_date(_col(row, mapping.date_col))
        if not date_str:
            continue

        # --- Description / Memo ---
        desc = _col(row, mapping.description_col)
        desc2 = _col(row, mapping.description2_col)

        # --- BAI code ---
        bai_code = _col(row, mapping.bai_code_col)

        # --- Balance / summary filter ---
        if is_balance_row(desc, bai_code) or is_balance_row(desc2, bai_code):
            continue

        # --- Amount ---
        if mapping.is_split_amount:
            credit = parse_amount(_col(row, mapping.credit_col))
            debit = parse_amount(_col(row, mapping.debit_col))
            if credit > 0:
                amount = credit
            elif debit > 0:
                amount = -debit
            else:
                continue
        elif mapping.amount_col is not None:
            amount = parse_amount(_col(row, mapping.amount_col))
            if amount == 0:
                continue
            # If there's a credit/debit indicator column, use it to set sign
            if mapping.cd_indicator_col is not None:
                indicator = _col(row, mapping.cd_indicator_col).upper()
                if "DEBIT" in indicator:
                    amount = -abs(amount)
                elif "CREDIT" in indicator:
                    amount = abs(amount)
            # If BAI code indicates debit range (400-699), force negative
            if bai_code:
                try:
                    bai_int = int(bai_code)
                    if 400 <= bai_int <= 699:
                        amount = -abs(amount)
                    elif 100 <= bai_int <= 399:
                        amount = abs(amount)
                except ValueError:
                    pass
        else:
            continue  # No amount column found; skip

        # --- Raw transaction type ---
        raw_tran = _col(row, mapping.tran_type_col)

        # --- Transaction type ---
        tran_type = map_transaction_type(desc, amount, bai_code, raw_tran)

        # --- Reference / Transaction ID ---
        ref = _col(row, mapping.reference_col)

        # --- Memo: use full REMARK chain if available, else description ---
        if mapping.remark_cols:
            memo = _build_remark_memo(row, mapping.remark_cols)
        else:
            memo = desc2 if desc2 and desc2 != desc else desc

        # --- Payee Name ---
        # Prefer the standard BAI description (e.g. "Preauthorized ACH Credit")
        # over the bank's generic DESCRIPTION (e.g. "EFT CREDIT").
        # Fall back to the DESCRIPTION column, then extracted payee from memo.
        if bai_code and bai_code in BAI_DESCRIPTION_MAP:
            payee = BAI_DESCRIPTION_MAP[bai_code]
        else:
            # Use the DESCRIPTION column as payee (not the remark)
            payee_source = desc if desc else (desc2 if desc2 else memo)
            payee = extract_payee(payee_source)

        # If check number is the reference column, mark as Check type
        if ref and mapping.reference_col is not None:
            if tran_type == "" and ref.isdigit() and len(ref) <= 6:
                tran_type = "Check"

        results.append([
            date_str,
            payee,
            ref,
            tran_type,
            f"{amount:.2f}",
            memo if memo else desc,
            "",  # NS Internal Customer Id
            "",  # NS Customer Name
            "",  # Invoice Number(s)
        ])

    return results


# ═══════════════════════════════════════════════════════════════════════════
# FILE READING  — skip preamble rows, find the real header
# ═══════════════════════════════════════════════════════════════════════════

PREAMBLE_PATTERNS = [
    re.compile(r"^From:\s", re.IGNORECASE),
    re.compile(r"^Exported\s", re.IGNORECASE),
    re.compile(r"^Selected\s+account", re.IGNORECASE),
    re.compile(r'^""$'),
    re.compile(r"^Showing results for", re.IGNORECASE),
    re.compile(r'^"?Showing results for', re.IGNORECASE),
]


def _is_preamble(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    # Lines that are all quotes, commas, and whitespace (empty CSV rows)
    if all(c in ' ,"\t\r\n' for c in stripped):
        return True
    # Also check with leading quotes stripped (some CSVs quote everything)
    unquoted = stripped.lstrip('"').strip()
    for pat in PREAMBLE_PATTERNS:
        if pat.match(stripped) or pat.match(unquoted):
            return True
    return False


def read_csv_smart(filepath: str):
    """
    Read a CSV, skip preamble, return (headers: list[str], data_rows: list[list[str]]).
    """
    with open(filepath, "r", encoding="utf-8-sig", errors="replace") as f:
        lines = f.readlines()

    # Find the header row (first non-preamble, non-empty line)
    header_idx = None
    for i, line in enumerate(lines):
        if not _is_preamble(line):
            header_idx = i
            break

    if header_idx is None:
        return [], []

    # Parse header
    headers = next(csv.reader([lines[header_idx]]))

    # Parse data rows
    data_rows = []
    for line in lines[header_idx + 1:]:
        if _is_preamble(line):
            continue
        try:
            row = next(csv.reader([line]))
            data_rows.append(row)
        except (StopIteration, csv.Error):
            continue

    return headers, data_rows


# ═══════════════════════════════════════════════════════════════════════════
# MAIN CONVERSION PIPELINE
# ═══════════════════════════════════════════════════════════════════════════

def extract_gl_number(filename: str) -> str:
    return os.path.basename(filename)[:4].strip()


def convert_file(filepath: str) -> tuple:
    """
    Convert one bank CSV → NetSuite rows.
    Returns (gl_number, netsuite_rows, mapping_repr, error_msg).
    """
    gl = extract_gl_number(filepath)
    basename = os.path.basename(filepath)

    try:
        headers, data_rows = read_csv_smart(filepath)
    except Exception as e:
        return gl, [], "", f"Read error: {e}"

    if not headers or not data_rows:
        return gl, [], "", f"No data in {basename}"

    # Dynamic mapping
    mapping = map_columns(headers, data_rows)

    if mapping.date_col is None:
        return gl, [], str(mapping), f"Could not find a date column in {basename}"

    if mapping.amount_col is None and not mapping.is_split_amount:
        return gl, [], str(mapping), f"Could not find amount column(s) in {basename}"

    # Parse
    try:
        rows = parse_rows(mapping, data_rows)
    except Exception as e:
        return gl, [], str(mapping), f"Parse error: {e}"

    return gl, rows, str(mapping), ""


def write_netsuite_csv(path: str, rows: list):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(NETSUITE_HEADERS)
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(
        description="Convert bank CSVs to NetSuite Bank Statement Import format"
    )
    ap.add_argument("--input-dir", required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    src = Path(args.input_dir)
    dst = Path(args.output_dir)
    if not src.exists():
        print(f"Error: {src} does not exist"); sys.exit(1)
    dst.mkdir(parents=True, exist_ok=True)

    csv_files = sorted(f for f in src.iterdir() if f.suffix.lower() == ".csv")
    if not csv_files:
        print(f"No CSV files in {src}"); sys.exit(1)

    print(f"Found {len(csv_files)} CSV files.\n")

    ok, empty, errors = [], [], []

    for fp in csv_files:
        gl, rows, mapping_info, err = convert_file(str(fp))

        if err:
            if "No data" in err:
                empty.append((fp.name, err))
            else:
                errors.append((fp.name, err, mapping_info))
            if args.verbose:
                tag = "EMPTY" if "No data" in err else "ERROR"
                print(f"  {tag:5s}  {fp.name}: {err}")
            continue

        if not rows:
            empty.append((fp.name, "No transactions after filtering"))
            if args.verbose:
                print(f"  EMPTY  {fp.name}: 0 transactions after filtering")
            continue

        out_name = f"{gl}.csv"
        write_netsuite_csv(str(dst / out_name), rows)
        ok.append((fp.name, gl, len(rows), mapping_info))

        if args.verbose:
            print(f"  OK     {fp.name} -> {out_name} ({len(rows)} txns)")

    # Summary
    print(f"\n{'=' * 60}")
    print("CONVERSION SUMMARY")
    print(f"{'=' * 60}")
    print(f"Successfully converted: {len(ok)} files")
    for name, gl, cnt, info in ok:
        print(f"  {gl}.csv <- {name} ({cnt} transactions)")
        if args.verbose:
            print(f"           {info}")

    if empty:
        print(f"\nEmpty / no transactions: {len(empty)} files")
        for name, msg in empty:
            print(f"  {name}: {msg}")

    if errors:
        print(f"\nErrors: {len(errors)} files")
        for name, msg, info in errors:
            print(f"  {name}: {msg}")
            if args.verbose and info:
                print(f"           {info}")

    print(f"\nOutput saved to: {dst}")


if __name__ == "__main__":
    main()
