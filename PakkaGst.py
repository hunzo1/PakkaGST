#!/usr/bin/env python3
"""
PakkaGst

GST invoicing from your terminal. No subscription, no cloud, no account.
Built because Vyapar/myBillBook want a yearly fee for something that's
mostly addition and a PDF, and I wasn't paying for that as a freelancer.

Everything is local: SQLite for storage, PDFs and QR codes generated
on-device. Just run `pakkagst` and answer what it asks.
"""

import csv
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

try:
    import questionary
    from questionary import Style as QStyle
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    import qrcode
    from qrcode.image.pure import PyPNGImage
    from fpdf import FPDF
except ImportError as e:
    sys.stderr.write(
        "\nPakkaGst is missing a dependency: {}\n\n"
        "Set it up with:\n"
        "  pkg install python-pillow   (Termux only, one-time)\n"
        "  pip install -e .\n\n".format(e)
    )
    sys.exit(1)

# paths — everything lives under ~/.pakkagst, nothing touches the cloud
APP_DIR = Path.home() / ".pakkagst"
APP_DIR.mkdir(exist_ok=True)
DB_PATH = APP_DIR / "pakkagst.db"
INVOICES_DIR = APP_DIR / "invoices"
INVOICES_DIR.mkdir(exist_ok=True)
ASSETS_DIR = APP_DIR / "assets"
ASSETS_DIR.mkdir(exist_ok=True)

DATA_FILE = Path(__file__).resolve().parent / "hsn_rates.json"

# Official GST state/UT codes (first two digits of a GSTIN).
STATE_CODES = {
    "01": "Jammu & Kashmir", "02": "Himachal Pradesh", "03": "Punjab",
    "04": "Chandigarh", "05": "Uttarakhand", "06": "Haryana", "07": "Delhi",
    "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam",
    "19": "West Bengal", "20": "Jharkhand", "21": "Odisha",
    "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "26": "Dadra & Nagar Haveli and Daman & Diu", "27": "Maharashtra",
    "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala",
    "33": "Tamil Nadu", "34": "Puducherry", "35": "Andaman & Nicobar Islands",
    "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
    "97": "Other Territory", "99": "Centre Jurisdiction",
}

QSTYLE = QStyle([
    ("qmark", "fg:#2bb673 bold"),
    ("question", "bold"),
    ("answer", "fg:#2bb673 bold"),
    ("pointer", "fg:#2bb673 bold"),
    ("highlighted", "fg:#2bb673 bold"),
    ("selected", "fg:#2bb673"),
])

console = Console()

TWOPLACES = Decimal("0.01")


def money(x) -> Decimal:
    return Decimal(str(x)).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def current_fy() -> str:
    today = date.today()
    start = today.year if today.month >= 4 else today.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


# amount-in-words + Indian-style digit grouping for the PDF. wrote these by
# hand instead of pulling in a dependency for something this small.

_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
         "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
         "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _two_digit_words(n):
    if n < 20:
        return _ONES[n]
    return _TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else "")


def _three_digit_words(n):
    if n >= 100:
        rest = _two_digit_words(n % 100)
        return _ONES[n // 100] + " Hundred" + (" " + rest if rest else "")
    return _two_digit_words(n)


def indian_number_words(n: int) -> str:
    if n == 0:
        return "Zero"
    parts = []
    crore, n = divmod(n, 10 ** 7)
    lakh, n = divmod(n, 10 ** 5)
    thousand, n = divmod(n, 1000)
    hundred = n
    if crore:
        parts.append(_three_digit_words(crore) + " Crore")
    if lakh:
        parts.append(_three_digit_words(lakh) + " Lakh")
    if thousand:
        parts.append(_three_digit_words(thousand) + " Thousand")
    if hundred:
        parts.append(_three_digit_words(hundred))
    return " ".join(parts)


def international_number_words(n: int) -> str:
    if n == 0:
        return "Zero"
    parts = []
    billion, n = divmod(n, 10 ** 9)
    million, n = divmod(n, 10 ** 6)
    thousand, n = divmod(n, 1000)
    hundred = n
    if billion:
        parts.append(_three_digit_words(billion) + " Billion")
    if million:
        parts.append(_three_digit_words(million) + " Million")
    if thousand:
        parts.append(_three_digit_words(thousand) + " Thousand")
    if hundred:
        parts.append(_three_digit_words(hundred))
    return " ".join(parts)


_CURRENCY_WORDS = {"INR": ("Rupees", "Paise"), "USD": ("US Dollars", "Cents"),
                    "EUR": ("Euros", "Cents"), "GBP": ("Pounds", "Pence")}


def amount_in_words(amount: Decimal, currency: str) -> str:
    amount = money(amount)
    whole = int(amount)
    minor = int((amount - whole) * 100)
    words_fn = indian_number_words if currency == "INR" else international_number_words
    name, minor_name = _CURRENCY_WORDS.get(currency, (currency, "Cents"))
    text = f"{name} {words_fn(whole)}"
    if minor:
        text += f" and {words_fn(minor)} {minor_name}"
    return text + " Only"


def indian_grouping(amount: Decimal) -> str:
    # 1,23,456.78 instead of 123,456.78 — lakh/crore grouping
    s = f"{amount:.2f}"
    whole, dec = s.split(".")
    neg = whole.startswith("-")
    whole = whole.lstrip("-")
    if len(whole) <= 3:
        grouped = whole
    else:
        last3, rest = whole[-3:], whole[:-3]
        chunks = []
        while len(rest) > 2:
            chunks.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            chunks.insert(0, rest)
        grouped = ",".join(chunks) + "," + last3
    return ("-" if neg else "") + grouped + "." + dec


def format_money(amount, currency: str) -> str:
    amount = Decimal(str(amount))
    return indian_grouping(amount) if currency == "INR" else f"{amount:,.2f}"


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _add_column_if_missing(conn, table, coldef):
    # SQLite has no "ADD COLUMN IF NOT EXISTS", so we just try it and
    # swallow the "duplicate column" error on an already-migrated DB.
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e).lower():
            raise


def init_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS business (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            name TEXT NOT NULL,
            is_gst_registered INTEGER NOT NULL DEFAULT 0,
            gstin TEXT,
            state_code TEXT NOT NULL,
            address TEXT,
            phone TEXT,
            upi_vpa TEXT,
            invoice_prefix TEXT NOT NULL DEFAULT 'INV',
            next_invoice_number INTEGER NOT NULL DEFAULT 1,
            next_quote_number INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS businesses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            is_gst_registered INTEGER NOT NULL DEFAULT 0,
            gstin TEXT,
            state_code TEXT NOT NULL,
            address TEXT,
            phone TEXT,
            upi_vpa TEXT,
            invoice_prefix TEXT NOT NULL DEFAULT 'INV',
            next_invoice_number INTEGER NOT NULL DEFAULT 1,
            next_quote_number INTEGER NOT NULL DEFAULT 1,
            next_credit_note_number INTEGER NOT NULL DEFAULT 1,
            next_challan_number INTEGER NOT NULL DEFAULT 1,
            is_active INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            gstin TEXT,
            state_code TEXT NOT NULL,
            address TEXT,
            phone TEXT,
            email TEXT
        );

        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_number TEXT UNIQUE NOT NULL,
            doc_type TEXT NOT NULL DEFAULT 'invoice',
            client_id INTEGER NOT NULL REFERENCES clients(id),
            issue_date TEXT NOT NULL,
            due_date TEXT,
            currency TEXT NOT NULL DEFAULT 'INR',
            fx_rate TEXT NOT NULL DEFAULT '1',
            subtotal TEXT NOT NULL,
            cgst TEXT NOT NULL DEFAULT '0',
            sgst TEXT NOT NULL DEFAULT '0',
            igst TEXT NOT NULL DEFAULT '0',
            total TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'unpaid',
            notes TEXT,
            pdf_path TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS document_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL REFERENCES documents(id),
            description TEXT NOT NULL,
            hsn_sac TEXT,
            quantity TEXT NOT NULL DEFAULT '1',
            rate TEXT NOT NULL,
            gst_rate TEXT NOT NULL DEFAULT '0',
            amount TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            client_id INTEGER REFERENCES clients(id),
            items_json TEXT NOT NULL,
            notes TEXT
        );
        """
    )

    # --- migrate the old single-row `business` table into `businesses`, once ---
    old = conn.execute("SELECT * FROM business WHERE id = 1").fetchone()
    already_migrated = conn.execute("SELECT COUNT(*) c FROM businesses").fetchone()["c"] > 0
    migrated_business_id = None
    if old and not already_migrated:
        cur = conn.execute(
            """INSERT INTO businesses
               (name, is_gst_registered, gstin, state_code, address, phone, upi_vpa,
                invoice_prefix, next_invoice_number, next_quote_number, is_active)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (old["name"], old["is_gst_registered"], old["gstin"], old["state_code"],
             old["address"], old["phone"], old["upi_vpa"], old["invoice_prefix"],
             old["next_invoice_number"], old["next_quote_number"]),
        )
        migrated_business_id = cur.lastrowid

    # a few columns added after the first release — safe no-ops if already there
    _add_column_if_missing(conn, "documents", "business_id INTEGER")
    _add_column_if_missing(conn, "documents", "reference_doc_number TEXT")
    _add_column_if_missing(conn, "documents", "invoice_subtype TEXT")
    _add_column_if_missing(conn, "documents", "vehicle_number TEXT")
    _add_column_if_missing(conn, "documents", "transport_reason TEXT")
    _add_column_if_missing(conn, "templates", "recurrence TEXT NOT NULL DEFAULT 'none'")
    _add_column_if_missing(conn, "templates", "next_due_date TEXT")
    _add_column_if_missing(conn, "templates", "currency TEXT NOT NULL DEFAULT 'INR'")
    _add_column_if_missing(conn, "templates", "due_days TEXT")
    _add_column_if_missing(conn, "templates", "fx_rate TEXT NOT NULL DEFAULT '1'")

    # old invoices predate the business_id column — point them at the
    # migrated profile so they don't vanish from the list
    if migrated_business_id:
        conn.execute("UPDATE documents SET business_id = ? WHERE business_id IS NULL",
                     (migrated_business_id,))

    conn.commit()
    conn.close()


def get_active_business(conn):
    row = conn.execute("SELECT * FROM businesses WHERE is_active = 1 LIMIT 1").fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM businesses ORDER BY id LIMIT 1").fetchone()
        if row:
            conn.execute("UPDATE businesses SET is_active = 1 WHERE id = ?", (row["id"],))
            conn.commit()
    return row


def list_businesses(conn):
    return conn.execute("SELECT * FROM businesses ORDER BY id").fetchall()


def switch_business(conn, business_id):
    conn.execute("UPDATE businesses SET is_active = 0")
    conn.execute("UPDATE businesses SET is_active = 1 WHERE id = ?", (business_id,))
    conn.commit()


def next_doc_number(conn, biz, doc_type):
    seq_map = {
        "invoice": ("next_invoice_number", biz["invoice_prefix"]),
        "quotation": ("next_quote_number", "QTN"),
        "credit_note": ("next_credit_note_number", "CN"),
        "delivery_challan": ("next_challan_number", "DC"),
    }
    seq_field, prefix = seq_map[doc_type]
    number = biz[seq_field]
    doc_number = f"{prefix}/{current_fy()}/{number:04d}"
    conn.execute(f"UPDATE businesses SET {seq_field} = {seq_field} + 1 WHERE id = ?", (biz["id"],))
    return doc_number


# ---- GST math ----
# Same state as the client -> split the tax 50/50 into CGST + SGST.
# Different state -> the whole thing is IGST instead. That's the entire
# place-of-supply rule that matters for a services freelancer; goods have
# more edge cases but we don't deal with goods here.

def compute_tax_split(taxable_amount: Decimal, gst_rate: Decimal,
                       seller_state: str, buyer_state: str,
                       gst_registered: bool) -> dict:
    if not gst_registered or gst_rate == 0:
        return {"cgst": Decimal("0"), "sgst": Decimal("0"), "igst": Decimal("0"),
                "tax_total": Decimal("0")}
    tax_total = money(taxable_amount * gst_rate / Decimal("100"))
    if seller_state == buyer_state:
        half = money(tax_total / 2)
        return {"cgst": half, "sgst": tax_total - half, "igst": Decimal("0"),
                "tax_total": tax_total}
    return {"cgst": Decimal("0"), "sgst": Decimal("0"), "igst": tax_total,
            "tax_total": tax_total}


def load_hsn_data() -> dict:
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def search_hsn(query: str) -> list:
    data = load_hsn_data()
    q = query.strip().lower()
    results = []
    for entry in data["codes"]:
        if q in entry["code"].lower() or q in entry["description"].lower() \
                or q in entry["category"].lower():
            results.append(entry)
    return results


def check_hsn_updates() -> str:
    # pulls the latest hsn_rates.json straight from the repo, replaces the
    # local copy if it's newer. no account, no telemetry, just a GET.
    data = load_hsn_data()
    local_version = data["_meta"]["version"]
    url = data["_meta"]["update_url"]
    try:
        with urllib.request.urlopen(url, timeout=6) as resp:
            remote = json.loads(resp.read().decode("utf-8"))
        remote_version = remote["_meta"]["version"]
        if remote_version != local_version:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(remote, f, indent=2)
            return f"Updated: {local_version} -> {remote_version}"
        return f"Already up to date ({local_version})."
    except Exception as e:
        return f"Could not check for updates right now ({e}). Still on {local_version}."


def build_upi_uri(vpa: str, payee_name: str, amount: Decimal, note: str) -> str:
    params = {
        "pa": vpa,
        "pn": payee_name,
        "am": f"{amount:.2f}",
        "cu": "INR",
        "tn": note[:50],
    }
    return "upi://pay?" + urllib.parse.urlencode(params)


def shorten_url(long_url: str) -> str:
    # TinyURL's old keyless endpoint, still works, no signup needed.
    # upi:// isn't a "real" http(s) URL so it can get rejected, and there's
    # no guarantee of network anyway — either way we just fall back to the
    # long link. Nothing about payment depends on this step succeeding.
    try:
        api = "https://tinyurl.com/api-create.php?" + urllib.parse.urlencode({"url": long_url})
        with urllib.request.urlopen(api, timeout=5) as resp:
            short = resp.read().decode("utf-8").strip()
            if short.startswith("http"):
                return short
    except Exception:
        pass
    return long_url


def generate_qr_png(data: str, out_path: Path) -> Path:
    # PyPNGImage keeps this Pillow-free, which matters on Termux where
    # Pillow means `pkg install python-pillow` first. this doesn't.
    img = qrcode.make(data, image_factory=PyPNGImage)
    img.save(str(out_path))
    return out_path


def build_whatsapp_link(phone: str, message: str) -> str:
    digits = re.sub(r"\D", "", phone)
    return "https://wa.me/" + digits + "?" + urllib.parse.urlencode({"text": message})


def build_pdf(conn, doc_row, client_row, items, out_path, qr_path, payment_link):
    biz = get_active_business(conn)
    pdf = FPDF(format="A4", unit="mm")
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    titles = {
        "quotation": "QUOTATION",
        "credit_note": "CREDIT NOTE",
    }
    if doc_row["doc_type"] == "invoice":
        if doc_row["invoice_subtype"] == "bill_of_supply":
            title = "BILL OF SUPPLY"
        elif biz["is_gst_registered"]:
            title = "TAX INVOICE"
        else:
            title = "INVOICE"
    else:
        title = titles.get(doc_row["doc_type"], doc_row["doc_type"].upper())

    # Header
    pdf.set_font("helvetica", "B", 16)
    pdf.cell(0, 9, biz["name"], ln=1)
    pdf.set_font("helvetica", "", 10)
    if biz["address"]:
        pdf.multi_cell(100, 5, biz["address"])
    if biz["phone"]:
        pdf.cell(0, 5, f"Phone: {biz['phone']}", ln=1)
    if biz["is_gst_registered"] and biz["gstin"]:
        pdf.cell(0, 5, f"GSTIN: {biz['gstin']}", ln=1)

    pdf.set_xy(130, 10)
    pdf.set_font("helvetica", "B", 14)
    pdf.cell(70, 8, title, align="R", ln=1)
    pdf.set_x(130)
    pdf.set_font("helvetica", "", 10)
    pdf.cell(70, 5, f"No: {doc_row['doc_number']}", align="R", ln=1)
    pdf.set_x(130)
    pdf.cell(70, 5, f"Date: {doc_row['issue_date']}", align="R", ln=1)
    if doc_row["due_date"]:
        pdf.set_x(130)
        pdf.cell(70, 5, f"Due: {doc_row['due_date']}", align="R", ln=1)
    if doc_row["reference_doc_number"]:
        pdf.set_x(130)
        pdf.cell(70, 5, f"Against: {doc_row['reference_doc_number']}", align="R", ln=1)

    pdf.ln(8)
    pdf.set_draw_color(180, 180, 180)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(4)

    # Bill To
    pdf.set_font("helvetica", "B", 11)
    pdf.cell(0, 6, "Bill To:", ln=1)
    pdf.set_font("helvetica", "", 10)
    pdf.cell(0, 5, client_row["name"], ln=1)
    if client_row["address"]:
        pdf.multi_cell(120, 5, client_row["address"])
    if client_row["gstin"]:
        pdf.cell(0, 5, f"GSTIN: {client_row['gstin']}", ln=1)
    pdf.cell(0, 5, f"State: {STATE_CODES.get(client_row['state_code'], client_row['state_code'])}", ln=1)

    pdf.ln(5)

    # Items table
    col_widths = [75, 22, 15, 25, 20, 33]
    headers = ["Description", "HSN/SAC", "Qty", "Rate", "GST%", "Amount"]
    pdf.set_font("helvetica", "B", 9)
    pdf.set_fill_color(235, 235, 235)
    for w, h in zip(col_widths, headers):
        pdf.cell(w, 7, h, border=1, align="C", fill=True)
    pdf.ln()

    pdf.set_font("helvetica", "", 9)
    currency = doc_row["currency"]
    currency_symbol = "Rs." if currency == "INR" else currency + " "
    for item in items:
        y_before = pdf.get_y()
        x_before = pdf.get_x()
        pdf.multi_cell(col_widths[0], 6, item["description"], border=1)
        y_after = pdf.get_y()
        row_h = max(y_after - y_before, 6)
        pdf.set_xy(x_before + col_widths[0], y_before)
        pdf.cell(col_widths[1], row_h, item["hsn_sac"] or "-", border=1, align="C")
        pdf.cell(col_widths[2], row_h, str(item["quantity"]), border=1, align="C")
        pdf.cell(col_widths[3], row_h, f"{currency_symbol}{format_money(item['rate'], currency)}", border=1, align="R")
        pdf.cell(col_widths[4], row_h, f"{Decimal(item['gst_rate']):.0f}%", border=1, align="C")
        pdf.cell(col_widths[5], row_h, f"{currency_symbol}{format_money(item['amount'], currency)}", border=1, align="R")
        pdf.set_xy(x_before, y_before + row_h)

    pdf.ln(4)

    # Totals block, right aligned
    def total_line(label, value, bold=False):
        pdf.set_x(120)
        pdf.set_font("helvetica", "B" if bold else "", 10)
        pdf.cell(45, 6, label)
        pdf.cell(35, 6, f"{currency_symbol}{format_money(value, currency)}", align="R", ln=1)

    total_line("Subtotal:", doc_row["subtotal"])
    if Decimal(doc_row["cgst"]) > 0:
        total_line("CGST:", doc_row["cgst"])
        total_line("SGST:", doc_row["sgst"])
    if Decimal(doc_row["igst"]) > 0:
        total_line("IGST:", doc_row["igst"])
    total_line("Total:", doc_row["total"], bold=True)

    pdf.set_x(10)
    pdf.set_font("helvetica", "I", 8)
    pdf.multi_cell(0, 5, "Amount in words: " + amount_in_words(Decimal(doc_row["total"]), currency))

    # Payment section
    if qr_path and qr_path.exists():
        pdf.ln(6)
        y = pdf.get_y()
        pdf.image(str(qr_path), x=10, y=y, w=30, h=30)
        pdf.set_xy(45, y + 5)
        pdf.set_font("helvetica", "B", 10)
        pdf.cell(0, 6, "Scan to pay via UPI", ln=1)
        pdf.set_x(45)
        pdf.set_font("helvetica", "", 8)
        if payment_link:
            pdf.multi_cell(140, 5, payment_link)
        pdf.set_y(y + 32)

    if doc_row["notes"]:
        pdf.ln(4)
        pdf.set_font("helvetica", "I", 9)
        pdf.multi_cell(0, 5, f"Notes: {doc_row['notes']}")

    pdf.set_y(-15)
    pdf.set_font("helvetica", "I", 7)
    pdf.set_text_color(150, 150, 150)
    pdf.cell(0, 5, "Made with PakkaGst (free, open source) - github.com/farymm/PakkaGst", align="C")

    pdf.output(str(out_path))
    return out_path


def banner():
    console.print(Panel.fit(
        Text("PakkaGst", style="bold green") + Text("  —  GST invoicing for Indian freelancers", style="dim"),
        border_style="green",
    ))


def ask_text(message, default="", validate=None):
    return questionary.text(message, default=default, validate=validate, style=QSTYLE).ask()


def ask_select(message, choices):
    return questionary.select(message, choices=choices, style=QSTYLE).ask()


def ask_confirm(message, default=True):
    return questionary.confirm(message, default=default, style=QSTYLE).ask()


def not_empty(text):
    return True if text.strip() else "This can't be empty."


def is_decimal(text):
    try:
        Decimal(text)
        return True
    except Exception:
        return "Enter a number."


def is_gstin(text):
    if not text:
        return True
    # 2-digit state + 10-char PAN + entity code + Z + checksum, always 15 chars
    pattern = r"^\d{2}[A-Z]{5}\d{4}[A-Z]{1}[1-9A-Z]{1}Z[0-9A-Z]{1}$"
    return True if re.match(pattern, text.strip().upper()) else \
        "Doesn't look like a valid 15-character GSTIN (e.g. 27AAAAA0000A1Z5)."


def run_setup(conn, existing=None):
    console.print(Panel("Let's set up your business profile." if existing is None
                         else "Editing this business profile.", border_style="green"))
    name = ask_text("Your business / freelancer name:", default=existing["name"] if existing else "",
                     validate=not_empty)
    gst_registered = ask_confirm("Are you GST-registered?",
                                  default=bool(existing["is_gst_registered"]) if existing else False)
    gstin = ""
    state_code = existing["state_code"] if existing else ""
    if gst_registered:
        gstin = ask_text("Your GSTIN:", default=existing["gstin"] if existing and existing["gstin"] else "",
                          validate=is_gstin).strip().upper()
        state_code = gstin[:2]
        console.print(f"  -> State detected from GSTIN: {STATE_CODES.get(state_code, 'unknown')}")
    else:
        console.print("No problem — you'll issue plain invoices with no GST fields.")
        state_choices = [f"{code} - {name_}" for code, name_ in sorted(STATE_CODES.items())]
        picked = ask_select("Your state (for your own records):", state_choices)
        state_code = picked.split(" - ")[0]

    address = ask_text("Business address (optional):", default=existing["address"] if existing and existing["address"] else "")
    phone = ask_text("Phone (optional):", default=existing["phone"] if existing and existing["phone"] else "")
    upi_vpa = ask_text("UPI ID / VPA for payment QR codes (optional, e.g. yourname@okhdfcbank):",
                        default=existing["upi_vpa"] if existing and existing["upi_vpa"] else "")
    prefix = ask_text("Invoice number prefix:", default=(existing["invoice_prefix"] if existing else "INV"),
                       validate=not_empty)

    if existing:
        conn.execute(
            """UPDATE businesses SET name=?, is_gst_registered=?, gstin=?, state_code=?,
               address=?, phone=?, upi_vpa=?, invoice_prefix=? WHERE id=?""",
            (name, int(gst_registered), gstin, state_code, address, phone, upi_vpa, prefix, existing["id"]),
        )
    else:
        # new profile — only the first one becomes active automatically
        is_first = conn.execute("SELECT COUNT(*) c FROM businesses").fetchone()["c"] == 0
        cur = conn.execute(
            """INSERT INTO businesses (name, is_gst_registered, gstin, state_code, address,
               phone, upi_vpa, invoice_prefix, is_active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, int(gst_registered), gstin, state_code, address, phone, upi_vpa, prefix, int(is_first)),
        )
        if not is_first and ask_confirm(f"Make '{name}' the active profile now?", default=True):
            switch_business(conn, cur.lastrowid)
    conn.commit()
    console.print("[green]Saved.[/green]")


def manage_businesses(conn):
    rows = list_businesses(conn)
    table = Table(title="Business Profiles")
    table.add_column("Active")
    table.add_column("Name")
    table.add_column("GSTIN")
    for r in rows:
        table.add_row("*" if r["is_active"] else "", r["name"], r["gstin"] or "-")
    console.print(table)
    options = ["Add a new business profile", "Switch active profile", "Edit active profile", "Back"]
    choice = ask_select("What do you want to do?", options)
    if choice == "Add a new business profile":
        run_setup(conn, existing=None)
    elif choice == "Switch active profile":
        names = [f"{r['id']}: {r['name']}" for r in rows]
        picked = ask_select("Switch to:", names)
        switch_business(conn, int(picked.split(":")[0]))
        console.print("[green]Switched.[/green]")
    elif choice == "Edit active profile":
        run_setup(conn, existing=get_active_business(conn))


def add_client(conn):
    name = ask_text("Client name / business:", validate=not_empty)
    has_gstin = ask_confirm("Does this client have a GSTIN?", default=False)
    gstin = ""
    if has_gstin:
        gstin = ask_text("Client GSTIN:", validate=is_gstin).strip().upper()
        state_code = gstin[:2]
    else:
        state_choices = [f"{code} - {name_}" for code, name_ in sorted(STATE_CODES.items())]
        picked = ask_select("Client's state (for place-of-supply):", state_choices)
        state_code = picked.split(" - ")[0]
    address = ask_text("Client address (optional):")
    phone = ask_text("Client phone (optional, for WhatsApp sharing):")
    email = ask_text("Client email (optional):")
    cur = conn.execute(
        "INSERT INTO clients (name, gstin, state_code, address, phone, email) VALUES (?, ?, ?, ?, ?, ?)",
        (name, gstin, state_code, address, phone, email),
    )
    conn.commit()
    console.print(f"[green]Client '{name}' added.[/green]")
    return cur.lastrowid


def pick_client(conn):
    rows = conn.execute("SELECT * FROM clients ORDER BY name").fetchall()
    choices = [f"{r['id']}: {r['name']}" for r in rows] + ["+ Add a new client"]
    if not rows:
        console.print("[yellow]No clients yet — let's add one.[/yellow]")
        return add_client(conn)
    picked = ask_select("Client:", choices)
    if picked == "+ Add a new client":
        return add_client(conn)
    return int(picked.split(":")[0])


def manage_clients(conn):
    rows = conn.execute("SELECT * FROM clients ORDER BY name").fetchall()
    if not rows:
        if ask_confirm("No clients yet. Add one now?"):
            add_client(conn)
        return
    table = Table(title="Clients")
    table.add_column("Name")
    table.add_column("State")
    table.add_column("GSTIN")
    table.add_column("Phone")
    for r in rows:
        table.add_row(r["name"], STATE_CODES.get(r["state_code"], r["state_code"]),
                       r["gstin"] or "-", r["phone"] or "-")
    console.print(table)
    if ask_confirm("Add another client?", default=False):
        add_client(conn)


def collect_items(conn):
    items = []
    console.print("[bold]Add line items[/bold] (enter a blank description to finish)")
    while True:
        desc = ask_text("Item description (blank to finish):")
        if not desc:
            break
        hsn = ""
        if ask_confirm("Look up an HSN/SAC code for this?", default=True):
            q = ask_text("Search HSN/SAC by keyword or code:")
            matches = search_hsn(q) if q else []
            if matches:
                choices = [f"{m['code']} - {m['description']} ({m['rate']}%)" for m in matches] + ["None of these / skip"]
                picked = ask_select("Pick a match:", choices)
                if picked != "None of these / skip":
                    hsn = picked.split(" - ")[0]
                    default_rate = picked.split("(")[-1].rstrip("%)")
                else:
                    default_rate = "18"
            else:
                console.print("[yellow]No matches found.[/yellow]")
                default_rate = "18"
        else:
            default_rate = "18"
        qty = Decimal(ask_text("Quantity:", default="1", validate=is_decimal))
        rate = Decimal(ask_text("Rate per unit:", validate=is_decimal))
        gst_rate = Decimal(ask_text("GST rate % for this item:", default=default_rate, validate=is_decimal))
        amount = money(qty * rate)
        items.append({
            "description": desc, "hsn_sac": hsn, "quantity": str(qty),
            "rate": str(rate), "gst_rate": str(gst_rate), "amount": str(amount),
        })
        console.print(f"  -> {desc}: {qty} x {rate} = {amount}")
    return items


def pick_reference_invoice(conn, biz):
    rows = conn.execute(
        """SELECT d.*, c.name AS client_name FROM documents d JOIN clients c ON c.id = d.client_id
           WHERE d.doc_type = 'invoice' AND d.business_id = ? ORDER BY d.created_at DESC""",
        (biz["id"],),
    ).fetchall()
    if not rows:
        console.print("[yellow]No invoices yet to credit against.[/yellow]")
        return None
    choices = [f"{r['doc_number']} — {r['client_name']} — {r['total']} {r['currency']}" for r in rows]
    picked = ask_select("Which invoice is this credit note against?", choices)
    doc_number = picked.split(" — ")[0]
    return next(r for r in rows if r["doc_number"] == doc_number)


def new_document(conn, doc_type: str, prefill_items=None, prefill_client_id=None,
                  silent=False, template_defaults=None):
    # silent=True is the one path with nobody at the keyboard to answer anything
    # (cron calling check_due_recurring). Every ask_*() below either has a
    # silent-safe branch or is skipped outright — a background run must never
    # sit there waiting on stdin that's never coming.
    biz = get_active_business(conn)
    template_defaults = template_defaults or {}

    reference_doc_number = None
    if doc_type == "credit_note":
        # the invoice already tells us the client — no point asking twice
        ref_row = pick_reference_invoice(conn, biz)
        if ref_row is None:
            return
        reference_doc_number = ref_row["doc_number"]
        client_id = ref_row["client_id"]
    else:
        client_id = prefill_client_id or pick_client(conn)
    client = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()

    if silent:
        currency = template_defaults.get("currency") or "INR"
        fx_rate = Decimal(template_defaults.get("fx_rate") or "1")
    else:
        currency = ask_text("Currency:", default="INR").strip().upper() or "INR"
        fx_rate = Decimal("1")
        if currency != "INR":
            fx_rate = Decimal(ask_text(f"Exchange rate (1 {currency} = ? INR):", validate=is_decimal))

    items = prefill_items if prefill_items else collect_items(conn)
    if not items:
        console.print("[red]No items added — cancelled.[/red]")
        return

    subtotal = sum(Decimal(i["amount"]) for i in items)
    # each line item carries its own GST rate and gets split individually,
    # then we add the pieces up — handles mixed-rate invoices correctly
    # instead of assuming one rate for the whole thing
    cgst = sgst = igst = Decimal("0")
    gst_registered = bool(biz["is_gst_registered"])
    for i in items:
        split = compute_tax_split(Decimal(i["amount"]), Decimal(i["gst_rate"]),
                                   biz["state_code"], client["state_code"], gst_registered)
        cgst += split["cgst"]
        sgst += split["sgst"]
        igst += split["igst"]
    total = money(subtotal + cgst + sgst + igst)

    # a GST-registered seller charging zero tax across the board is issuing
    # an exempt supply — legally that's a Bill of Supply, not a Tax Invoice
    invoice_subtype = None
    if doc_type == "invoice" and gst_registered:
        invoice_subtype = "bill_of_supply" if (cgst + sgst + igst) == 0 else "tax_invoice"
        if invoice_subtype == "bill_of_supply":
            console.print("[dim]No tax on this one — generating it as a Bill of Supply.[/dim]")

    due_date = ""
    if doc_type == "invoice":
        if silent:
            due_days = template_defaults.get("due_days")
            if due_days:
                due_date = (date.today() + timedelta(days=int(Decimal(due_days)))).isoformat()
        elif ask_confirm("Set a due date?", default=True):
            days = ask_text("Due in how many days?", default="7", validate=is_decimal)
            due_date = (date.today() + timedelta(days=int(Decimal(days)))).isoformat()

    notes = "" if silent else ask_text("Notes to print on the document (optional):")
    doc_number = next_doc_number(conn, biz, doc_type)

    cur = conn.execute(
        """INSERT INTO documents
           (doc_number, doc_type, client_id, business_id, issue_date, due_date, currency, fx_rate,
            subtotal, cgst, sgst, igst, total, status, notes, created_at, reference_doc_number,
            invoice_subtype)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unpaid', ?, ?, ?, ?)""",
        (doc_number, doc_type, client_id, biz["id"], date.today().isoformat(), due_date, currency,
         str(fx_rate), str(subtotal), str(cgst), str(sgst), str(igst), str(total), notes,
         datetime.now().isoformat(), reference_doc_number, invoice_subtype),
    )
    doc_id = cur.lastrowid
    for i in items:
        conn.execute(
            """INSERT INTO document_items (document_id, description, hsn_sac, quantity, rate, gst_rate, amount)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (doc_id, i["description"], i["hsn_sac"], i["quantity"], i["rate"], i["gst_rate"], i["amount"]),
        )
    conn.commit()

    doc_row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()

    qr_path = None
    payment_link = None
    if doc_type == "invoice" and biz["upi_vpa"]:
        uri = build_upi_uri(biz["upi_vpa"], biz["name"], Decimal(doc_row["total"]), doc_number)
        qr_path = ASSETS_DIR / f"{doc_id}_qr.png"
        try:
            generate_qr_png(uri, qr_path)
        except Exception as e:
            # QR generation is a nice-to-have, not the invoice itself — if it
            # breaks for whatever reason (missing dep, weird env) the invoice
            # should still go out, just without the code. Tell the human why.
            qr_path = None
            if not silent:
                console.print(f"[yellow]Couldn't generate the QR code ({e}) — invoice will still be made, just without it.[/yellow]")
        if silent:
            # no network call with nobody around to notice if it hangs —
            # the raw link always works, shortening is just cosmetic
            payment_link = uri
        elif ask_confirm("Shorten the payment link for sharing (uses a free online shortener)?", default=True):
            payment_link = shorten_url(uri)
        else:
            payment_link = uri

    safe_name = doc_number.replace("/", "-")
    pdf_path = INVOICES_DIR / f"{safe_name}.pdf"
    build_pdf(conn, doc_row, client, items, pdf_path, qr_path, payment_link)
    conn.execute("UPDATE documents SET pdf_path = ? WHERE id = ?", (str(pdf_path), doc_id))
    conn.commit()

    console.print(Panel(
        f"[bold green]{doc_number}[/bold green] saved -> {pdf_path}\nTotal: {format_money(total, currency)} {currency}",
        border_style="green"))

    if (not silent) and client["phone"] and ask_confirm("Build a WhatsApp share message for this client?", default=True):
        msg = f"Hi {client['name']}, here's your {doc_type.replace('_', ' ')} {doc_number} for {format_money(total, currency)} {currency}."
        if payment_link:
            msg += f" Pay here: {payment_link}"
        wa_link = build_whatsapp_link(client["phone"], msg)
        console.print(f"[cyan]{wa_link}[/cyan]")

    if silent:
        # this run came FROM a template already — just keep its currency/fx/
        # due-days current for next time, no prompts, no new template offer
        if "template_name" in template_defaults:
            due_days_used = template_defaults.get("due_days")
            conn.execute(
                "UPDATE templates SET currency = ?, fx_rate = ?, due_days = ? WHERE name = ?",
                (currency, str(fx_rate), due_days_used, template_defaults["template_name"]),
            )
            conn.commit()
    elif doc_type == "invoice" and ask_confirm("Save this as a reusable template?", default=False):
        tname = ask_text("Template name:", validate=not_empty)
        due_days = None
        if due_date:
            due_days = str((date.fromisoformat(due_date) - date.today()).days)
        # plain INSERT OR REPLACE would wipe recurrence/next_due_date on a
        # template re-save under the same name — upsert only what changed
        conn.execute(
            """INSERT INTO templates (name, client_id, items_json, notes, currency, fx_rate, due_days)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   client_id=excluded.client_id, items_json=excluded.items_json,
                   notes=excluded.notes, currency=excluded.currency,
                   fx_rate=excluded.fx_rate, due_days=excluded.due_days""",
            (tname, client_id, json.dumps(items), notes, currency, str(fx_rate), due_days),
        )
        conn.commit()
        console.print(f"[green]Template '{tname}' saved.[/green]")
        if ask_confirm("Make this template recur automatically (weekly/monthly/yearly)?", default=False):
            set_template_recurrence(conn, tname)


def new_delivery_challan(conn):
    # challans move goods, they don't sell anything — no tax, no payment
    # section, just what's being moved, why, and on what vehicle
    biz = get_active_business(conn)
    client_id = pick_client(conn)
    client = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()

    items = []
    console.print("[bold]What's being moved?[/bold] (blank description to finish)")
    while True:
        desc = ask_text("Item description (blank to finish):")
        if not desc:
            break
        qty = ask_text("Quantity:", default="1", validate=is_decimal)
        items.append({"description": desc, "hsn_sac": "", "quantity": qty, "rate": "0",
                       "gst_rate": "0", "amount": "0"})
    if not items:
        console.print("[red]Nothing added — cancelled.[/red]")
        return

    reason = ask_select("Reason for transport:",
                         ["Job work", "Sample / exhibition", "Return of goods", "Other"])
    vehicle = ask_text("Vehicle number (optional):")
    notes = ask_text("Notes (optional):")

    doc_number = next_doc_number(conn, biz, "delivery_challan")
    cur = conn.execute(
        """INSERT INTO documents
           (doc_number, doc_type, client_id, business_id, issue_date, currency, subtotal,
            total, status, notes, created_at, vehicle_number, transport_reason)
           VALUES (?, 'delivery_challan', ?, ?, ?, 'INR', '0', '0', 'n/a', ?, ?, ?, ?)""",
        (doc_number, client_id, biz["id"], date.today().isoformat(), notes,
         datetime.now().isoformat(), vehicle, reason),
    )
    doc_id = cur.lastrowid
    for i in items:
        conn.execute(
            """INSERT INTO document_items (document_id, description, hsn_sac, quantity, rate, gst_rate, amount)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (doc_id, i["description"], i["hsn_sac"], i["quantity"], i["rate"], i["gst_rate"], i["amount"]),
        )
    conn.commit()
    doc_row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()

    safe_name = doc_number.replace("/", "-")
    pdf_path = INVOICES_DIR / f"{safe_name}.pdf"
    build_challan_pdf(conn, doc_row, client, items, pdf_path)
    conn.execute("UPDATE documents SET pdf_path = ? WHERE id = ?", (str(pdf_path), doc_id))
    conn.commit()
    console.print(Panel(f"[bold green]{doc_number}[/bold green] saved -> {pdf_path}", border_style="green"))


def build_challan_pdf(conn, doc_row, client_row, items, out_path):
    biz = get_active_business(conn)
    pdf = FPDF(format="A4", unit="mm")
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    pdf.set_font("helvetica", "B", 16)
    pdf.cell(0, 9, biz["name"], ln=1)
    pdf.set_font("helvetica", "", 10)
    if biz["address"]:
        pdf.multi_cell(100, 5, biz["address"])

    pdf.set_xy(130, 10)
    pdf.set_font("helvetica", "B", 14)
    pdf.cell(70, 8, "DELIVERY CHALLAN", align="R", ln=1)
    pdf.set_x(130)
    pdf.set_font("helvetica", "", 10)
    pdf.cell(70, 5, f"No: {doc_row['doc_number']}", align="R", ln=1)
    pdf.set_x(130)
    pdf.cell(70, 5, f"Date: {doc_row['issue_date']}", align="R", ln=1)

    pdf.ln(8)
    pdf.set_draw_color(180, 180, 180)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(4)

    pdf.set_font("helvetica", "B", 11)
    pdf.cell(0, 6, "Deliver To:", ln=1)
    pdf.set_font("helvetica", "", 10)
    pdf.cell(0, 5, client_row["name"], ln=1)
    if client_row["address"]:
        pdf.multi_cell(120, 5, client_row["address"])

    pdf.ln(3)
    pdf.set_font("helvetica", "", 10)
    pdf.cell(0, 5, f"Reason for transport: {doc_row['transport_reason'] or '-'}", ln=1)
    if doc_row["vehicle_number"]:
        pdf.cell(0, 5, f"Vehicle: {doc_row['vehicle_number']}", ln=1)

    pdf.ln(5)
    pdf.set_font("helvetica", "B", 9)
    pdf.set_fill_color(235, 235, 235)
    pdf.cell(150, 7, "Description", border=1, align="C", fill=True)
    pdf.cell(40, 7, "Quantity", border=1, align="C", fill=True)
    pdf.ln()
    pdf.set_font("helvetica", "", 9)
    for item in items:
        pdf.cell(150, 7, item["description"], border=1)
        pdf.cell(40, 7, str(item["quantity"]), border=1, align="C")
        pdf.ln()

    if doc_row["notes"]:
        pdf.ln(4)
        pdf.set_font("helvetica", "I", 9)
        pdf.multi_cell(0, 5, f"Notes: {doc_row['notes']}")

    pdf.set_y(-15)
    pdf.set_font("helvetica", "I", 7)
    pdf.set_text_color(150, 150, 150)
    pdf.cell(0, 5, "Made with PakkaGst (free, open source) - github.com/farymm/PakkaGst", align="C")
    pdf.output(str(out_path))
    return out_path


def from_template(conn):
    rows = conn.execute("SELECT * FROM templates ORDER BY name").fetchall()
    if not rows:
        console.print("[yellow]No templates saved yet.[/yellow]")
        return
    choices = [r["name"] for r in rows]
    picked = ask_select("Template:", choices)
    row = next(r for r in rows if r["name"] == picked)
    items = json.loads(row["items_json"])
    console.print(f"Loaded {len(items)} item(s) from template '{picked}'. You'll confirm the client and dates next.")
    new_document(conn, "invoice", prefill_items=items, prefill_client_id=row["client_id"])


def list_documents(conn):
    biz = get_active_business(conn)
    rows = conn.execute(
        """SELECT d.*, c.name AS client_name FROM documents d
           JOIN clients c ON c.id = d.client_id
           WHERE d.business_id = ? ORDER BY d.created_at DESC""",
        (biz["id"],),
    ).fetchall()
    if not rows:
        console.print("[yellow]Nothing yet.[/yellow]")
        return
    today = date.today().isoformat()
    table = Table(title=f"Documents for {biz['name']}")
    table.add_column("No.")
    table.add_column("Type")
    table.add_column("Client")
    table.add_column("Total")
    table.add_column("Status")
    for r in rows:
        status = r["status"]
        if status == "n/a":
            status = "-"
        elif status == "unpaid" and r["due_date"] and r["due_date"] < today:
            status = "[red]overdue[/red]"
        elif status == "paid":
            status = "[green]paid[/green]"
        else:
            status = "[yellow]unpaid[/yellow]"
        table.add_row(r["doc_number"], r["doc_type"], r["client_name"],
                      f"{format_money(r['total'], r['currency'])} {r['currency']}", status)
    console.print(table)

    if ask_confirm("Mark one of these paid?", default=False):
        num = ask_text("Document number to mark paid:")
        conn.execute("UPDATE documents SET status = 'paid' WHERE doc_number = ?", (num,))
        conn.commit()
        console.print("[green]Updated.[/green]")


def hsn_menu():
    q = ask_text("Search HSN/SAC codes by keyword or code:", validate=not_empty)
    matches = search_hsn(q)
    if not matches:
        console.print("[yellow]No matches in the local table.[/yellow]")
        return
    table = Table(title=f"Matches for '{q}'")
    table.add_column("Code")
    table.add_column("Description")
    table.add_column("Rate")
    table.add_column("Category")
    for m in matches:
        table.add_row(m["code"], m["description"], f"{m['rate']}%", m["category"])
    console.print(table)


# ---- recurring templates ----
# "Automatic" here means two things stacked: check_due_recurring() runs on
# every launch no matter what, so nothing gets silently missed even on a
# phone that's never running in the background. install_cron() is the extra
# layer for anyone on a machine that's actually always-on (a VPS, a laptop
# left running) — it's opt-in, and if there's no crontab around it just
# says so and backs off instead of failing.

def _bump_due_date(d: date, recurrence: str) -> date:
    if recurrence == "weekly":
        return d + timedelta(days=7)
    if recurrence == "monthly":
        month = d.month + 1
        year = d.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
        days_in_month = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
        day = min(d.day, days_in_month[month - 1])
        return date(year, month, day)
    if recurrence == "yearly":
        try:
            return d.replace(year=d.year + 1)
        except ValueError:
            return d.replace(year=d.year + 1, day=28)  # Feb 29 on a non-leap year
    return d


def set_template_recurrence(conn, template_name):
    choice = ask_select(f"How often should '{template_name}' recur?",
                         ["Weekly", "Monthly", "Yearly", "Turn off recurrence"])
    if choice == "Turn off recurrence":
        conn.execute("UPDATE templates SET recurrence = 'none', next_due_date = NULL WHERE name = ?",
                     (template_name,))
    else:
        recurrence = choice.lower()
        next_due = _bump_due_date(date.today(), recurrence)
        conn.execute("UPDATE templates SET recurrence = ?, next_due_date = ? WHERE name = ?",
                     (recurrence, next_due.isoformat(), template_name))
    conn.commit()
    console.print("[green]Updated.[/green]")


def manage_recurring_templates(conn):
    rows = conn.execute("SELECT * FROM templates ORDER BY name").fetchall()
    if not rows:
        console.print("[yellow]No templates saved yet — save an invoice as a template first.[/yellow]")
        return
    recurring = [r for r in rows if r["recurrence"] != "none"]
    if recurring:
        table = Table(title="Recurring Templates")
        table.add_column("Template")
        table.add_column("Every")
        table.add_column("Next Due")
        for r in recurring:
            table.add_row(r["name"], r["recurrence"], r["next_due_date"] or "-")
        console.print(table)
    else:
        console.print("[yellow]None of your templates recur yet.[/yellow]")
    if ask_confirm("Set or change recurrence on a template?", default=False):
        name = ask_select("Which template?", [r["name"] for r in rows])
        set_template_recurrence(conn, name)


def check_due_recurring(conn, silent=False):
    today = date.today().isoformat()
    due = conn.execute(
        """SELECT * FROM templates WHERE recurrence != 'none' AND next_due_date IS NOT NULL
           AND next_due_date <= ?""",
        (today,),
    ).fetchall()
    for row in due:
        if not silent and not ask_confirm(
                f"'{row['name']}' was due on {row['next_due_date']} — generate it now?", default=True):
            continue
        if silent:
            console.print(f"[dim]Auto-generating recurring invoice from '{row['name']}'...[/dim]")
        items = json.loads(row["items_json"])
        defaults = {"template_name": row["name"], "currency": row["currency"],
                    "fx_rate": row["fx_rate"], "due_days": row["due_days"]}
        new_document(conn, "invoice", prefill_items=items, prefill_client_id=row["client_id"],
                     silent=silent, template_defaults=defaults)
        next_due = _bump_due_date(date.fromisoformat(row["next_due_date"]), row["recurrence"])
        conn.execute("UPDATE templates SET next_due_date = ? WHERE id = ?", (next_due.isoformat(), row["id"]))
        conn.commit()


def install_cron(conn):
    pakkagst_bin = shutil.which("pakkagst")
    if not pakkagst_bin:
        pakkagst_bin = f"{sys.executable} {Path(__file__).resolve()}"
    if not shutil.which("crontab"):
        console.print("[yellow]No 'crontab' in this environment (normal on plain Termux) — that's fine, "
                       "nothing's broken. PakkaGst will still catch up on due recurring invoices the "
                       "next time you open it, just not while it's closed.[/yellow]")
        return
    try:
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        if "pakkagst" in existing.lower():
            console.print("[yellow]A PakkaGst cron entry already looks like it's installed — leaving it alone.[/yellow]")
            return
        line = f"0 9 * * * {pakkagst_bin} --process-due >> {APP_DIR}/cron.log 2>&1\n"
        subprocess.run(["crontab", "-"], input=existing + line, text=True, check=True)
        console.print("[green]Done — this machine will check for due recurring invoices daily at 9am, "
                       "even with PakkaGst closed.[/green]")
    except Exception as e:
        console.print(f"[yellow]Couldn't install the cron job ({e}). Not a dealbreaker — recurring "
                       "invoices still get caught up next time you open the tool yourself.[/yellow]")


# ---- GST return summary (GSTR-1 / GSTR-3B outward-supply side) ----

def export_gst_return(conn):
    biz = get_active_business(conn)
    if not biz["is_gst_registered"]:
        console.print("[yellow]This profile isn't GST-registered, so there's no return to file.[/yellow]")
        return
    period = ask_text("Which month? (YYYY-MM, e.g. 2026-11):", validate=not_empty).strip()
    rows = conn.execute(
        """SELECT d.*, c.name AS client_name, c.gstin AS client_gstin FROM documents d
           JOIN clients c ON c.id = d.client_id
           WHERE d.business_id = ? AND d.doc_type IN ('invoice', 'credit_note')
             AND d.issue_date LIKE ?""",
        (biz["id"], period + "%"),
    ).fetchall()
    if not rows:
        console.print(f"[yellow]Nothing found for {period}.[/yellow]")
        return

    reports_dir = APP_DIR / "reports"
    reports_dir.mkdir(exist_ok=True)

    summary_path = reports_dir / f"gst_summary_{period}.csv"
    with open(summary_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Doc Number", "Type", "Date", "Client", "Client GSTIN", "B2B/B2C",
                    "Taxable Value", "CGST", "SGST", "IGST", "Total"])
        for r in rows:
            sign = -1 if r["doc_type"] == "credit_note" else 1
            w.writerow([
                r["doc_number"], r["doc_type"], r["issue_date"], r["client_name"],
                r["client_gstin"] or "", "B2B" if r["client_gstin"] else "B2C",
                sign * Decimal(r["subtotal"]), sign * Decimal(r["cgst"]),
                sign * Decimal(r["sgst"]), sign * Decimal(r["igst"]), sign * Decimal(r["total"]),
            ])

    hsn_summary = {}
    for r in rows:
        sign = -1 if r["doc_type"] == "credit_note" else 1
        items = conn.execute("SELECT * FROM document_items WHERE document_id = ?", (r["id"],)).fetchall()
        for it in items:
            key = (it["hsn_sac"] or "UNSPECIFIED", it["gst_rate"])
            agg = hsn_summary.setdefault(key, {"qty": Decimal("0"), "value": Decimal("0"), "tax": Decimal("0")})
            agg["qty"] += sign * Decimal(it["quantity"])
            agg["value"] += sign * Decimal(it["amount"])
            agg["tax"] += sign * money(Decimal(it["amount"]) * Decimal(it["gst_rate"]) / Decimal("100"))

    hsn_path = reports_dir / f"gst_hsn_summary_{period}.csv"
    with open(hsn_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["HSN/SAC", "GST Rate", "Total Quantity", "Total Taxable Value", "Total Tax"])
        for (hsn, rate), agg in sorted(hsn_summary.items()):
            w.writerow([hsn, f"{rate}%", agg["qty"], agg["value"], agg["tax"]])

    console.print(Panel(
        f"Saved:\n  {summary_path}\n  {hsn_path}\n\n"
        "Scope, honestly: this is your outward supplies — what GSTR-1 and the outward side of "
        "GSTR-3B need, straight from your invoices and credit notes. It does NOT include input "
        "tax credit, because this tool doesn't track your purchases, so it's not a complete "
        "GSTR-3B net-liability figure on its own. Hand this to your CA alongside your purchase "
        "records, don't file straight off it.",
        title="GST Return Summary", border_style="green"))


MENU = [
    "New Invoice",
    "New Quotation",
    "New Credit Note",
    "New Delivery Challan",
    "Generate from Template",
    "View Invoices / Track Payments",
    "Manage Clients",
    "HSN/SAC Lookup",
    "Check for GST Rate Table Updates",
    "Recurring Templates",
    "Export GST Return Summary (GSTR-1/3B)",
    "Business Profiles",
    "Enable Daily Auto-Check (cron)",
    "Exit",
]


def _process_due_and_exit():
    # the non-interactive path a cron job calls — no prompts, no menu,
    # just catch up on whatever's due and get out. never used by a human
    # typing commands; see install_cron() for where this comes from.
    init_db()
    conn = get_db()
    if get_active_business(conn) is not None:
        check_due_recurring(conn, silent=True)
    conn.close()


def main():
    if "--process-due" in sys.argv:
        _process_due_and_exit()
        return

    init_db()
    conn = get_db()
    banner()

    biz = get_active_business(conn)
    if biz is None:
        run_setup(conn)
        biz = get_active_business(conn)

    check_due_recurring(conn)

    while True:
        choice = ask_select("What would you like to do?", MENU)
        if choice is None or choice == "Exit":
            console.print("[dim]Bye.[/dim]")
            break
        elif choice == "New Invoice":
            new_document(conn, "invoice")
        elif choice == "New Quotation":
            new_document(conn, "quotation")
        elif choice == "New Credit Note":
            new_document(conn, "credit_note")
        elif choice == "New Delivery Challan":
            new_delivery_challan(conn)
        elif choice == "Generate from Template":
            from_template(conn)
        elif choice == "View Invoices / Track Payments":
            list_documents(conn)
        elif choice == "Manage Clients":
            manage_clients(conn)
        elif choice == "HSN/SAC Lookup":
            hsn_menu()
        elif choice == "Check for GST Rate Table Updates":
            console.print(check_hsn_updates())
        elif choice == "Recurring Templates":
            manage_recurring_templates(conn)
        elif choice == "Export GST Return Summary (GSTR-1/3B)":
            export_gst_return(conn)
        elif choice == "Business Profiles":
            manage_businesses(conn)
        elif choice == "Enable Daily Auto-Check (cron)":
            install_cron(conn)

    conn.close()


if __name__ == "__main__":
    main()
