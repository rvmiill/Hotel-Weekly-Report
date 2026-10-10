#!/usr/bin/env python3
"""
Weekly hotel performance report.

Pipeline:  Google Sheet -> benchmark & classify each hotel -> AI-written message -> email

Run:
    python weekly_report.py               # normal run
    DRY_RUN=true python weekly_report.py  # saves emails to ./outbox instead of sending

Settings come from environment variables (or a local .env file). See .env.example.
"""
import csv
import io
import json
import os
import re
import smtplib
import ssl
import statistics
import sys
import urllib.request
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path


# --------------------------------------------------------------------------
# 0. Config
# --------------------------------------------------------------------------
def load_dotenv(path=".env"):
    p = Path(path)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                v = v.strip()
                v = "" if v.startswith("#") else re.split(r"\s+#", v)[0]  # drop inline comments
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv()
ENV = os.environ.get

# Column headers in the sheet (defaults match the provided mock database)
COL_HOTEL = ENV("COL_HOTEL", "brandname")
COL_REVENUE = ENV("COL_REVENUE", "revenue")
COL_ORDERS = ENV("COL_ORDERS", "Order")
COL_EMAIL = ENV("COL_EMAIL", "Boss Gmail")
# Optional: if the sheet ever gets goal columns, they are used instead of peer benchmarks.
COL_REVENUE_TARGET = ENV("COL_REVENUE_TARGET", "revenue_target")
COL_ORDERS_TARGET = ENV("COL_ORDERS_TARGET", "order_target")

CURRENCY = ENV("CURRENCY", "")          # e.g. "NT$", "RM", "$"
MODEL = ENV("CLAUDE_MODEL", "claude-sonnet-5-5")

# Thresholds (reasoning in WRITEUP.md). Values are a share of the peer benchmark / goal.
STRONG_AT = float(ENV("STRONG_AT", "1.00"))   # at or above benchmark on BOTH revenue and bookings
WEAK_BELOW = float(ENV("NEEDS_BELOW", "0.75"))  # more than 25% below benchmark on EITHER
AOV_OK = float(ENV("AOV_OK", "0.90"))         # spend-per-booking considered healthy at >=90% of peers


# --------------------------------------------------------------------------
# 1. Pull data
# --------------------------------------------------------------------------
def fetch_rows():
    local = ENV("LOCAL_CSV")
    if local:
        text = Path(local).read_text(encoding="utf-8-sig")
    else:
        sheet_id = ENV("SHEET_ID")
        if not sheet_id:
            sys.exit("Set SHEET_ID (the long ID in the Google Sheet URL), or LOCAL_CSV=data/hotel_data.csv.")
        gid = ENV("SHEET_GID", "")
        # Accept a pasted full URL as well as a bare ID
        m = re.search(r"/d/([A-Za-z0-9_-]+)", sheet_id)
        if m:
            g = re.search(r"gid=(\d+)", sheet_id)
            sheet_id, gid = m.group(1), (g.group(1) if g else gid)
        gid = re.sub(r"\D", "", gid)  # empty = first tab
        # The sheet must be shared as "Anyone with the link can view"
        base = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
        urls = ([base + f"&gid={gid}"] if gid else []) + [base]  # fall back to the first tab
        text, err = None, None
        for url in urls:
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    text = r.read().decode("utf-8-sig")
                break
            except urllib.error.HTTPError as e:
                err = e
        if text is None:
            e = err
            sys.exit(f"Google returned HTTP {e.code} for sheet ID '{sheet_id}' (gid '{gid or 'first tab'}').\n"
                     "Check: SHEET_ID is only the ID (or the full URL), SHEET_GID is the number after 'gid=' "
                     "in the URL, and sharing is 'Anyone with the link can view'. If the file is an uploaded "
                     "Excel/CSV, open it in Google Sheets and use File > Save as Google Sheets first.\n"
                     "Fallback: File > Download > CSV, save as data/hotel_data.csv and set LOCAL_CSV=data/hotel_data.csv.")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        sys.exit("The sheet is empty.")
    need = [COL_HOTEL, COL_REVENUE, COL_ORDERS, COL_EMAIL]
    missing = [c for c in need if c not in rows[0]]
    if missing:
        sys.exit(f"Columns not found: {missing}. Found: {list(rows[0].keys())}. "
                 "Fix the COL_* settings in .env.")
    return rows


def to_number(v):
    s = re.sub(r"[^0-9.\-]", "", str(v or ""))
    return float(s) if s not in ("", "-", ".") else 0.0


def short_name(brand):
    """'松河商旅 Songhe Inn' -> 'Songhe Inn' (English part for the email); falls back to full name."""
    m = re.search(r"[A-Za-z][A-Za-z0-9 &'.\-]*$", brand.strip())
    return m.group(0).strip() if m else brand.strip()


def last_week_label():
    today = datetime.strptime(ENV("REPORT_DATE"), "%Y-%m-%d").date() if ENV("REPORT_DATE") else date.today()
    monday = today - timedelta(days=today.weekday()) - timedelta(days=7)
    sunday = monday + timedelta(days=6)
    return f"{monday:%d %b} - {sunday:%d %b %Y}"


# --------------------------------------------------------------------------
# 2. Analyse & decide good vs needs improvement (deterministic - no AI here)
# --------------------------------------------------------------------------
def ratio(a, b):
    return None if not b else a / b


def classify(rev_idx, ord_idx, orders):
    if orders == 0:
        return "NEEDS_IMPROVEMENT"
    worst = min(x for x in (rev_idx, ord_idx) if x is not None)
    if worst < WEAK_BELOW:
        return "NEEDS_IMPROVEMENT"
    if worst >= STRONG_AT:
        return "STRONG"
    return "ON_TRACK"


def diagnose(orders, ord_idx, aov_idx):
    """The one thing most worth working on, derived purely from the numbers."""
    if orders == 0:
        return "no_bookings"
    low_orders = ord_idx is not None and ord_idx < WEAK_BELOW
    low_aov = aov_idx is not None and aov_idx < WEAK_BELOW
    if low_orders and low_aov:
        return "volume_and_value"
    if low_orders and (aov_idx is None or aov_idx >= AOV_OK):
        return "booking_volume"
    if low_aov and (ord_idx is None or ord_idx >= AOV_OK):
        return "value_per_booking"
    if low_orders:
        return "booking_volume"
    if low_aov:
        return "value_per_booking"
    return "maintain"


FOCUS_TEXT = {
    "no_bookings": "No bookings and no revenue were recorded for the week.",
    "booking_volume": "Average spend per booking is healthy, but the number of bookings is below peers. Focus: attract more bookings (visibility, promotions, channel mix).",
    "value_per_booking": "Booking count is fine, but average spend per booking is well below peers. Focus: raise value per booking (room upgrades, packages, add-ons, pricing).",
    "volume_and_value": "Both booking count and spend per booking are below peers.",
    "maintain": "Performing at or near peer level on volume and spend per booking.",
}


def analyse(rows):
    hotels = []
    for r in rows:
        brand = (r.get(COL_HOTEL) or "").strip()
        if not brand:
            continue
        hotels.append({
            "brand": brand, "email": (r.get(COL_EMAIL) or "").strip(),
            "revenue": to_number(r.get(COL_REVENUE)), "orders": to_number(r.get(COL_ORDERS)),
            "rev_target": to_number(r.get(COL_REVENUE_TARGET)) if COL_REVENUE_TARGET in r else 0,
            "ord_target": to_number(r.get(COL_ORDERS_TARGET)) if COL_ORDERS_TARGET in r else 0,
        })
    active = [h for h in hotels if h["orders"] > 0]
    # Benchmarks = typical (median) active hotel; robust to one big or one dead outlier
    bench_rev = statistics.median(h["revenue"] for h in active)
    bench_ord = statistics.median(h["orders"] for h in active)
    bench_aov = statistics.median(h["revenue"] / h["orders"] for h in active)
    ranked = sorted(hotels, key=lambda h: -h["revenue"])

    for h in hotels:
        goal_rev = h["rev_target"] or bench_rev
        goal_ord = h["ord_target"] or bench_ord
        h["rev_idx"], h["ord_idx"] = ratio(h["revenue"], goal_rev), ratio(h["orders"], goal_ord)
        h["aov"] = h["revenue"] / h["orders"] if h["orders"] else 0
        h["aov_idx"] = ratio(h["aov"], bench_aov) if h["orders"] else None
        h["status"] = classify(h["rev_idx"], h["ord_idx"], h["orders"])
        h["focus"] = diagnose(h["orders"], h["ord_idx"], h["aov_idx"])
        h["rank"] = ranked.index(h) + 1
    bench = {"revenue": bench_rev, "orders": bench_ord, "aov": bench_aov, "n": len(hotels)}
    return hotels, bench


def build_facts(h, bench, week):
    money = lambda x: f"{CURRENCY}{x:,.0f}"
    pc = lambda x: None if x is None else f"{x * 100:.0f}%"
    return {
        "hotel": short_name(h["brand"]),
        "week": week,
        "status": h["status"],
        "focus": h["focus"],
        "focus_note": FOCUS_TEXT[h["focus"]],
        # Pre-formatted: the AI must quote these verbatim and never compute its own numbers.
        "display": {
            "bookings": f"{h['orders']:,.0f}",
            "revenue": money(h["revenue"]),
            "revenue_vs_goal": pc(h["rev_idx"]),
            "bookings_vs_goal": pc(h["ord_idx"]),
            "goal_revenue": money(bench["revenue"]),
            "goal_bookings": f"{bench['orders']:,.0f}",
            "avg_spend_per_booking": money(h["aov"]) if h["orders"] else None,
            "avg_spend_vs_peers": pc(h["aov_idx"]),
            "revenue_rank": f"{h['rank']} of {bench['n']}",
        },
        "goal_basis": "typical (median) active hotel in the partner network",
    }


# --------------------------------------------------------------------------
# 3. Write the message (AI) with a safe template fallback
# --------------------------------------------------------------------------
SYSTEM_PROMPT = """You write the Monday-morning weekly performance email from a hotel-partner team to a hotel owner.

You receive a JSON object of verified facts. Rules:
- Use ONLY the facts given. Quote figures exactly as written in "display". Never calculate, round, estimate or invent numbers, causes, events or promises. The "goal" is the typical (median) hotel in the partner network; say so plainly, e.g. "the typical partner hotel", not "your target".
- Open with "Dear <hotel> team," (use the "hotel" field). Plain text only, no markdown, no bullet symbols, under 150 words.
- Tone follows "status":
  STRONG: warm and celebratory; name what went well (bookings, revenue, rank); encourage keeping momentum.
  ON_TRACK: positive but honest that they are slightly under the typical hotel; point to the weaker metric.
  NEEDS_IMPROVEMENT: respectful, direct and constructive. State the gap plainly without blame or sugar-coating, then offer a call to plan next steps.
- "focus" and "focus_note" say what to work on. For NEEDS_IMPROVEMENT/ON_TRACK, give one or two practical suggestions that fit that focus only. Do not claim to know why it happened.
- If focus is "no_bookings": do not assume a cause. Say no bookings were recorded, ask whether this reflects a real issue (closure, availability, listing or reporting problem), and offer help.
- Always mention bookings and revenue. Skip any field that is null.
- Sign off as "The Partner Performance Team".

Return ONLY valid JSON: {"subject": "...", "body": "..."}"""


def template_message(f):
    d = f["display"]
    lead = {
        "STRONG": "Congratulations - a strong week, at or above the typical partner hotel on both bookings and revenue.",
        "ON_TRACK": "A steady week, slightly under the typical partner hotel.",
        "NEEDS_IMPROVEMENT": "This week came in clearly below the typical partner hotel, and we'd like to help turn it around.",
    }[f["status"]]
    body = f"Dear {f['hotel']} team,\n\n{lead}\n\nWeek of {f['week']}:\n"
    body += f"- Bookings: {d['bookings']} ({d['bookings_vs_goal']} of the typical hotel's {d['goal_bookings']})\n"
    body += f"- Revenue: {d['revenue']} ({d['revenue_vs_goal']} of the typical hotel's {d['goal_revenue']})\n"
    body += f"- Revenue rank: {d['revenue_rank']}\n"
    if f["focus"] != "maintain":
        body += f"\n{f['focus_note']}\n"
    if f["status"] == "NEEDS_IMPROVEMENT":
        body += "\nLet's schedule a short call this week to plan next steps.\n"
    body += "\nThe Partner Performance Team"
    return {"subject": f"{f['hotel']}: weekly performance report ({f['week']})", "body": body}


def validate(msg, f):
    """Guardrail: the message must contain the exact verified bookings and revenue figures."""
    if not isinstance(msg, dict):
        return False
    d = f["display"]
    norm = lambda s: re.sub(r"[,\s]", "", str(s))  # ignore thousands separators / spacing
    body = norm(msg.get("body", ""))
    return bool(msg.get("subject")) and norm(d["bookings"]) in body and norm(d["revenue"]) in body


def ai_message(f):
    if not ENV("ANTHROPIC_API_KEY"):
        print("   [info] no ANTHROPIC_API_KEY - using template message")
        return template_message(f)
    try:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=MODEL, max_tokens=2000, system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(f, ensure_ascii=False, indent=2)}],
        )
        # The reply may start with a "thinking" block; only use the text blocks.
        raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        text = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
        msg = json.loads(text)
        if validate(msg, f):
            return msg
        d = f["display"]
        print(f"   [warn] AI message failed validation (expected bookings '{d['bookings']}' and "
              f"revenue '{d['revenue']}'). Claude returned: {text[:400]!r} - using template")
    except Exception as e:
        print(f"   [warn] AI step failed ({e}) - using template")
    return template_message(f)


# --------------------------------------------------------------------------
# 4. Send
# --------------------------------------------------------------------------
def deliver(msg, owner_email, brand):
    real = ENV("SEND_TO_REAL_OWNERS", "false").lower() == "true"
    to = owner_email if real else ENV("TEST_RECIPIENT", "")
    subject = msg["subject"] if real else f"[TEST - would go to {owner_email}] {msg['subject']}"
    user, pw = ENV("SMTP_USER"), ENV("SMTP_APP_PASSWORD")

    dry = ENV("DRY_RUN", "false").lower() == "true"
    if dry or not (user and pw and to):
        why = ("DRY_RUN is 'true'" if dry else
               "missing: " + ", ".join(n for n, v in (("SMTP_USER", user), ("SMTP_APP_PASSWORD", pw),
                                                      ("TEST_RECIPIENT", to)) if not v))
        print(f"   [not sent] {why}")
        out = Path("outbox")
        out.mkdir(exist_ok=True)
        fn = out / (re.sub(r"\W+", "_", short_name(brand)) + ".txt")
        fn.write_text(f"To: {to or owner_email}\nSubject: {subject}\n\n{msg['body']}\n", encoding="utf-8")
        print(f"   [saved] {fn}")
        return

    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = user, to, subject
    m.set_content(msg["body"])
    with smtplib.SMTP_SSL(ENV("SMTP_HOST", "smtp.gmail.com"), int(ENV("SMTP_PORT", "465")),
                          context=ssl.create_default_context()) as s:
        s.login(user, pw)
        s.send_message(m)
    print(f"   [sent] -> {to}")


# --------------------------------------------------------------------------
def main():
    hotels, bench = analyse(fetch_rows())
    week = last_week_label()
    print(f"Week {week} | {len(hotels)} hotels | goal = median active hotel: "
          f"revenue {bench['revenue']:,.0f}, bookings {bench['orders']:,.0f}")

    Path("outbox").mkdir(exist_ok=True)
    with open("outbox/summary.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["hotel", "revenue", "bookings", "revenue_vs_goal", "bookings_vs_goal", "status", "focus"])
        for h in hotels:
            w.writerow([h["brand"], h["revenue"], h["orders"], f"{(h['rev_idx'] or 0):.2f}",
                        f"{(h['ord_idx'] or 0):.2f}", h["status"], h["focus"]])

    for h in hotels:
        print(f"- {short_name(h['brand'])}: {h['status']} ({h['focus']})")
        deliver(ai_message(build_facts(h, bench, week)), h["email"], h["brand"])


if __name__ == "__main__":
    main()