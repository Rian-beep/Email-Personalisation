"""
ConstructReach — UK construction outbound pipeline.

Built as Section 7 of the Lumina Head of Outreach screening submission.

Pipeline:
1. Find top UK construction companies (Perplexity)
2. Identify decision-makers (Companies House)
3. Verify emails (Prospeo)
4. Pull company context (Perplexity)
5. Generate personalised outreach (Anthropic Haiku)

Stack: Streamlit + SQLite + direct API calls.
"""

import os
import io
import csv
import json
import time
import sqlite3
from datetime import datetime
from pathlib import Path

import streamlit as st
import requests
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# Configuration
# ============================================================

DB_PATH = Path(__file__).parent / "data" / "constructreach.db"
DB_PATH.parent.mkdir(exist_ok=True)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY")
PROSPEO_API_KEY = os.getenv("PROSPEO_API_KEY")
COMPANIES_HOUSE_API_KEY = os.getenv("COMPANIES_HOUSE_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# ============================================================
# Database
# ============================================================

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_name TEXT NOT NULL,
            company_number TEXT,
            website TEXT,
            sector TEXT,
            director_name TEXT,
            director_role TEXT,
            first_name TEXT,
            last_name TEXT,
            email TEXT,
            email_status TEXT,
            company_context TEXT,
            subject TEXT,
            body TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER,
            stage TEXT,
            status TEXT,
            detail TEXT,
            occurred_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def log(company_id, stage, status, detail=""):
    conn = get_conn()
    conn.execute(
        "INSERT INTO pipeline_log (company_id, stage, status, detail) VALUES (?, ?, ?, ?)",
        (company_id, stage, status, detail[:500])
    )
    conn.commit()
    conn.close()


def update_company(company_id, **fields):
    if not fields:
        return
    conn = get_conn()
    cols = ", ".join(f"{k} = ?" for k in fields.keys())
    cols += ", updated_at = CURRENT_TIMESTAMP"
    values = list(fields.values()) + [company_id]
    conn.execute(f"UPDATE companies SET {cols} WHERE id = ?", values)
    conn.commit()
    conn.close()


def insert_company(company_name, sector="Construction"):
    """Insert a company if it doesn't already exist (dedup by case-insensitive name).
    Returns the company id (existing or new) and a flag for whether it was newly created."""
    conn = get_conn()
    # Check for existing match (case-insensitive, trim whitespace)
    existing = conn.execute(
        "SELECT id FROM companies WHERE LOWER(TRIM(company_name)) = LOWER(TRIM(?))",
        (company_name,)
    ).fetchone()
    if existing:
        conn.close()
        return existing["id"], False

    cursor = conn.execute(
        "INSERT INTO companies (company_name, sector, status) VALUES (?, ?, 'pending')",
        (company_name, sector)
    )
    company_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return company_id, True


def get_all_companies():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM companies ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_logs(limit=50):
    conn = get_conn()
    rows = conn.execute(
        "SELECT pl.*, c.company_name FROM pipeline_log pl "
        "LEFT JOIN companies c ON pl.company_id = c.id "
        "ORDER BY pl.id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def reset_db():
    if DB_PATH.exists():
        DB_PATH.unlink()
    init_db()


# ============================================================
# Stage 1: Find UK construction companies via Perplexity
# ============================================================

def find_construction_companies(target_count=10):
    """Use Perplexity to find top UK construction companies."""
    prompt = f"""List exactly {target_count} of the largest UK general contractors and construction companies. 
Return ONLY a JSON array with this exact format, no other text:
[
  {{"company_name": "Balfour Beatty", "website": "balfourbeatty.com", "company_number": ""}},
  ...
]
Focus on top-tier UK general contractors (e.g. Balfour Beatty, Skanska UK, Kier, Laing O'Rourke, Mace, Multiplex, Costain, Bouygues UK, Sir Robert McAlpine, Wates, BAM, Morgan Sindall, Galliford Try, Vinci UK, ISG, Bouygues Construction, Keller Group, Murphy Group, John Sisk, Willmott Dixon).
Return real company names and their primary domains. No commentary."""

    response = requests.post(
        "https://api.perplexity.ai/chat/completions",
        headers={
            "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": "sonar",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1500
        },
        timeout=60
    )
    response.raise_for_status()
    data = response.json()
    content = data["choices"][0]["message"]["content"]

    # Strip code fences if present
    content = content.replace("```json", "").replace("```", "").strip()

    # Find the JSON array
    start = content.find("[")
    end = content.rfind("]") + 1
    if start == -1 or end == 0:
        raise ValueError(f"No JSON array in Perplexity response: {content[:200]}")

    companies = json.loads(content[start:end])
    return companies


# ============================================================
# Stage 2: Find decision-maker via Companies House
# ============================================================

def search_companies_house(company_name):
    """Search Companies House for a company by name. Returns first match."""
    response = requests.get(
        "https://api.company-information.service.gov.uk/search/companies",
        auth=(COMPANIES_HOUSE_API_KEY, ""),
        params={"q": company_name, "items_per_page": 5},
        timeout=30
    )
    response.raise_for_status()
    items = response.json().get("items", [])
    if not items:
        return None

    # Prefer active companies
    active = [i for i in items if i.get("company_status") == "active"]
    return active[0] if active else items[0]


def get_company_officers(company_number):
    """Get current directors of a company."""
    response = requests.get(
        f"https://api.company-information.service.gov.uk/company/{company_number}/officers",
        auth=(COMPANIES_HOUSE_API_KEY, ""),
        params={"register_type": "directors", "items_per_page": 20},
        timeout=30
    )
    response.raise_for_status()
    return response.json().get("items", [])


def pick_top_officer(officers):
    """Pick the most relevant decision-maker from a list of officers."""
    relevant_titles = [
        "director", "managing", "commercial", "operations",
        "plant", "equipment", "sustainability", "esg", "ceo", "chief"
    ]

    # Filter to active officers with relevant roles
    active = [
        o for o in officers
        if not o.get("resigned_on")
        and any(
            kw in (o.get("officer_role", "") + " " + o.get("occupation", "")).lower()
            for kw in relevant_titles
        )
    ]

    if not active:
        return None

    # Prefer specific roles in this priority
    priority_keywords = ["managing director", "ceo", "chief", "managing", "director"]
    for kw in priority_keywords:
        for officer in active:
            role_text = (officer.get("officer_role", "") + " " + officer.get("occupation", "")).lower()
            if kw in role_text:
                return officer

    return active[0]


def parse_officer_name(full_name):
    """Companies House returns names as 'SURNAME, Firstname Middle'. Parse it."""
    if "," in full_name:
        parts = full_name.split(",")
        last_name = parts[0].strip()
        first_name = parts[1].strip().split(" ")[0] if len(parts) > 1 else ""
    else:
        parts = full_name.split(" ")
        first_name = parts[0]
        last_name = " ".join(parts[1:]) if len(parts) > 1 else ""

    # Title-case (Companies House returns surname in CAPS)
    last_name = last_name.title()
    first_name = first_name.title()
    return first_name, last_name


# ============================================================
# Stage 3: Verify email via Prospeo
# ============================================================

def find_email(first_name, last_name, company_name, company_website=""):
    """Use Prospeo's enrich-person endpoint to find a verified email."""
    # Prefer company_website if available, fall back to company_name
    data_payload = {
        "first_name": first_name,
        "last_name": last_name,
    }
    if company_website:
        data_payload["company_website"] = company_website
    else:
        data_payload["company_name"] = company_name

    response = requests.post(
        "https://api.prospeo.io/enrich-person",
        headers={
            "X-KEY": PROSPEO_API_KEY,
            "Content-Type": "application/json"
        },
        json={"data": data_payload},
        timeout=60
    )

    # Don't raise on 400 — NO_MATCH is expected sometimes
    body = response.json()

    if body.get("error"):
        return {
            "email": "",
            "status": body.get("error_code", "ERROR"),
            "raw": body
        }

    person = body.get("person", {}) or {}
    email_obj = person.get("email") or {}

    return {
        "email": email_obj.get("email", ""),
        "status": email_obj.get("status", "UNKNOWN"),
        "raw": body
    }


# ============================================================
# Stage 4: Get company context via Perplexity
# ============================================================

def get_company_context(company_name):
    prompt = f"""Provide a brief 3-4 sentence overview of {company_name}, a UK construction company. Focus on:
- Recent major projects they're working on
- Any contract wins in the last 6-12 months
- Public commitments around carbon reduction, fleet electrification, or sustainability
- Recent leadership changes if any

Be specific with names, project values, and dates where possible. No fluff."""

    response = requests.post(
        "https://api.perplexity.ai/chat/completions",
        headers={
            "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
            "Content-Type": "application/json"
        },
        json={
            "model": "sonar",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 400
        },
        timeout=60
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


# ============================================================
# Stage 5: Generate personalised email via Anthropic
# ============================================================

def generate_email(company_name, director_name, director_role, company_context):
    # Use just first name for greeting if we can
    first_name = director_name.split()[0] if director_name else "there"

    prompt = f"""You are writing a cold outreach email on behalf of Lumina, a Series A company building autonomous electric heavy equipment for the UK construction industry. Lumina's product replaces diesel-powered earthmovers, dumpers, and yard plant. Key benefits: lower carbon footprint, lower running cost, no operator fatigue, 24/7 operation in controlled environments.

Company: {company_name}
Decision-maker: {director_name}, {director_role}

Company context:
{company_context}

Write a short cold email matching this exact format and tone:

EXAMPLE (use this structure):
Subject: Sizewell C - same design, different equipment?

Hi Roger,

Caught your ICE talk on Hinkley last month.

Given Sizewell C carries the same design and same cooling system challenges, the team will arrive with everything Hinkley taught you, but the surface logistics and the Avonmouth-scale yard work are still diesel-heavy.

Lumina builds autonomous electric heavy equipment for exactly that kind of high-cycle, controlled-environment work.

Worth a 20-minute call before Sizewell mobilisation kicks in?

Best regards,

Rian

REQUIREMENTS:
- Subject must be short (under 8 words), reference a specific signal or project
- Body must be 4-5 short sentences, each on its own line/paragraph
- Open by referencing one specific recent signal from the company context
- Connect it to Lumina's product (autonomous electric heavy equipment for high-cycle controlled-environment work)
- End with a concrete call-to-action with specific timing
- Sign off "Best regards, Rian"
- No "I hope this finds you well", no fluff, no generic openers
- Sound like a senior operator, not marketing copy

Return ONLY a JSON object in this exact format:
{{"subject": "...", "body": "Hi {first_name},\\n\\n[opening line referencing specific signal]\\n\\n[paragraph connecting to Lumina]\\n\\n[short paragraph on the fit]\\n\\n[call-to-action question]\\n\\nBest regards,\\n\\nRian"}}

The body must contain literal \\n\\n between paragraphs (these are newline escape sequences in JSON). No other text outside the JSON."""

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json"
        },
        json={
            "model": "claude-haiku-4-5",
            "max_tokens": 600,
            "messages": [{"role": "user", "content": prompt}]
        },
        timeout=60
    )
    response.raise_for_status()
    data = response.json()
    text = data["content"][0]["text"]

    # Parse the JSON
    text = text.replace("```json", "").replace("```", "").strip()
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        return {"subject": "Parse error", "body": text}

    parsed = json.loads(text[start:end])
    return {
        "subject": parsed.get("subject", ""),
        "body": parsed.get("body", "")
    }


# ============================================================
# Main pipeline
# ============================================================

def run_pipeline_for_company(company_id, company_name, website, status_callback=None):
    """Run the full pipeline for one company, updating the DB at each stage."""
    def cb(msg):
        if status_callback:
            status_callback(msg)
        log(company_id, "pipeline", "info", msg)

    try:
        # ----- Stage 2: Companies House lookup -----
        cb(f"Searching Companies House for {company_name}")
        ch_match = search_companies_house(company_name)
        if not ch_match:
            update_company(company_id, status="ch_not_found")
            cb(f"❌ {company_name} not found in Companies House")
            return False

        company_number = ch_match.get("company_number", "")
        update_company(company_id, company_number=company_number)
        cb(f"✓ Found {company_name} (#{company_number})")

        # ----- Stage 2b: get directors -----
        cb(f"Pulling directors")
        officers = get_company_officers(company_number)
        top_officer = pick_top_officer(officers)
        if not top_officer:
            update_company(company_id, status="no_director_found")
            cb(f"❌ No relevant director found")
            return False

        first_name, last_name = parse_officer_name(top_officer.get("name", ""))
        director_name = f"{first_name} {last_name}".strip()
        director_role = top_officer.get("officer_role", "")
        update_company(
            company_id,
            director_name=director_name,
            first_name=first_name,
            last_name=last_name,
            director_role=director_role,
            status="director_found"
        )
        cb(f"✓ Director: {director_name} ({director_role})")

        # ----- Stage 3: Prospeo email -----
        cb(f"Finding email via Prospeo")
        email_result = find_email(first_name, last_name, company_name, website)
        update_company(
            company_id,
            email=email_result["email"],
            email_status=email_result["status"],
            website=website
        )
        if email_result["email"]:
            cb(f"✓ Email: {email_result['email']} ({email_result['status']})")
        else:
            cb(f"⚠ No email — {email_result['status']}")

        # ----- Stage 4: Perplexity context -----
        cb(f"Pulling company context")
        context = get_company_context(company_name)
        update_company(company_id, company_context=context)
        cb(f"✓ Context retrieved ({len(context)} chars)")

        # ----- Stage 5: Generate email -----
        cb(f"Generating personalised email")
        email_content = generate_email(company_name, director_name, director_role, context)
        update_company(
            company_id,
            subject=email_content["subject"],
            body=email_content["body"],
            status="complete"
        )
        cb(f"✓ Email generated: {email_content['subject'][:60]}")

        return True

    except Exception as e:
        update_company(company_id, status=f"error: {str(e)[:100]}")
        log(company_id, "pipeline", "error", str(e))
        cb(f"❌ Error: {str(e)[:100]}")
        return False


# ============================================================
# Streamlit UI
# ============================================================

def main():
    st.set_page_config(
        page_title="ConstructReach",
        page_icon="🏗️",
        layout="wide",
        initial_sidebar_state="collapsed"
    )

    # Custom CSS for dark Gotham aesthetic
    st.markdown("""
    <style>
        .stApp {
            background: #0A0A0A;
            color: #E5E5E5;
        }
        .stButton button {
            background: #1A1A1A;
            color: #FFF;
            border: 1px solid #333;
            border-radius: 0;
        }
        .stButton button:hover {
            background: #2563EB;
            border-color: #2563EB;
        }
        h1, h2, h3 {
            color: #FFF;
            font-weight: 500;
        }
        .stDataFrame {
            background: #0A0A0A;
        }
        div[data-testid="stMetricValue"] {
            font-size: 28px;
            color: #FFF;
        }
        div[data-testid="stMetricLabel"] {
            color: #888;
            text-transform: uppercase;
            font-size: 11px;
            letter-spacing: 0.05em;
        }
        .live-indicator {
            display: inline-block;
            color: #10B981;
            font-size: 12px;
            margin-left: 12px;
        }
        .activity-item {
            padding: 8px 12px;
            border-bottom: 1px solid #1F1F1F;
            font-size: 13px;
            color: #BBB;
        }
        .activity-time {
            color: #666;
            font-size: 11px;
        }
    </style>
    """, unsafe_allow_html=True)

    init_db()

    # Header
    col1, col2 = st.columns([3, 1])
    with col1:
        st.markdown("### ConstructReach / Campaigns / UK Construction — Top 20 General Contractors  <span class='live-indicator'>● LIVE</span>", unsafe_allow_html=True)
    with col2:
        if st.button("🔄 Reset", use_container_width=True):
            reset_db()
            st.rerun()

    # Stats row
    companies = get_all_companies()
    total = len(companies)
    with_director = sum(1 for c in companies if c.get("director_name"))
    with_email = sum(1 for c in companies if c.get("email"))
    completed = sum(1 for c in companies if c.get("status") == "complete")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Companies", total)
    c2.metric("Directors", with_director)
    c3.metric("Emails", with_email)
    c4.metric("Outreach Generated", completed)

    st.markdown("---")

    # Control panel
    st.markdown("#### Pipeline Control")
    col_a, col_b, col_c = st.columns([2, 2, 6])
    with col_a:
        target_count = st.number_input("Companies to find", min_value=1, max_value=20, value=5, step=1)
    with col_b:
        st.write("")
        st.write("")
        run_button = st.button("▶ Run Pipeline", use_container_width=True, type="primary")

    activity_placeholder = st.empty()

    if run_button:
        with activity_placeholder.container():
            st.markdown("#### Activity Feed")
            log_area = st.empty()
            messages = []

            def status_callback(msg):
                ts = datetime.now().strftime("%H:%M:%S")
                messages.insert(0, f"`{ts}` {msg}")
                log_area.markdown("\n\n".join(messages[:30]))

            # Stage 1: find companies via Perplexity
            status_callback("🔍 Finding top UK construction companies via Perplexity")
            try:
                discovered = find_construction_companies(target_count)
                status_callback(f"✓ Found {len(discovered)} companies")
            except Exception as e:
                status_callback(f"❌ Failed to find companies: {str(e)[:100]}")
                return

            # Insert and process each
            for c in discovered:
                cname = c.get("company_name", "").strip()
                website = c.get("website", "").strip()
                if not cname:
                    continue

                cid, is_new = insert_company(cname)
                if not is_new:
                    status_callback(f"⏭ Skipping {cname} — already in database")
                    continue
                update_company(cid, website=website)
                status_callback(f"━━━ Processing {cname} ━━━")
                run_pipeline_for_company(cid, cname, website, status_callback=status_callback)

            status_callback("✓ Pipeline complete")
            st.success("Pipeline complete. Scroll down to see results.")

    # Results table
    st.markdown("---")
    st.markdown("#### Results")

    companies = get_all_companies()
    if not companies:
        st.info("No companies yet. Click 'Run Pipeline' to start.")
        return

    # Use st.dataframe for the overview
    overview_data = [{
        "Company": c["company_name"],
        "Director": c.get("director_name") or "—",
        "Role": c.get("director_role") or "—",
        "Email": c.get("email") or "—",
        "Status": c.get("status") or "—",
    } for c in companies]

    st.dataframe(overview_data, use_container_width=True, height=300)

    # Export controls
    export_col1, export_col2, export_col3 = st.columns([2, 2, 6])

    with export_col1:
        # Build CSV of all results
        csv_buffer = io.StringIO()
        writer = csv.DictWriter(csv_buffer, fieldnames=[
            "company_name", "company_number", "website",
            "director_name", "director_role",
            "email", "email_status",
            "subject", "body",
            "company_context", "status"
        ])
        writer.writeheader()
        for c in companies:
            writer.writerow({
                "company_name": c.get("company_name", ""),
                "company_number": c.get("company_number", ""),
                "website": c.get("website", ""),
                "director_name": c.get("director_name", ""),
                "director_role": c.get("director_role", ""),
                "email": c.get("email", ""),
                "email_status": c.get("email_status", ""),
                "subject": c.get("subject", ""),
                "body": c.get("body", ""),
                "company_context": c.get("company_context", ""),
                "status": c.get("status", ""),
            })
        csv_data = csv_buffer.getvalue()

        st.download_button(
            label="📥 Export all (CSV)",
            data=csv_data,
            file_name=f"constructreach_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
            use_container_width=True
        )

    with export_col2:
        # CSV of just verified emails
        verified = [c for c in companies if c.get("email") and c.get("email_status") == "VERIFIED"]
        csv_verified_buffer = io.StringIO()
        writer_v = csv.DictWriter(csv_verified_buffer, fieldnames=[
            "company_name", "director_name", "director_role",
            "email", "subject", "body"
        ])
        writer_v.writeheader()
        for c in verified:
            writer_v.writerow({
                "company_name": c.get("company_name", ""),
                "director_name": c.get("director_name", ""),
                "director_role": c.get("director_role", ""),
                "email": c.get("email", ""),
                "subject": c.get("subject", ""),
                "body": c.get("body", ""),
            })

        st.download_button(
            label=f"📥 Verified only ({len(verified)})",
            data=csv_verified_buffer.getvalue(),
            file_name=f"constructreach_verified_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
            mime="text/csv",
            use_container_width=True,
            disabled=len(verified) == 0
        )

    # Detail expanders for each completed company
    st.markdown("#### Generated Outreach")
    completed_companies = [c for c in companies if c.get("subject")]

    for c in completed_companies:
        with st.expander(f"**{c['company_name']}** — {c.get('director_name', '?')} ({c.get('director_role', '—')})"):
            # Top metadata row
            meta_left, meta_right = st.columns([1, 1])
            with meta_left:
                st.markdown(f"**Email:** `{c.get('email') or 'Not found'}`")
                st.markdown(f"**Status:** `{c.get('email_status', '—')}`")
            with meta_right:
                if c.get('company_number'):
                    st.markdown(f"**Companies House:** [#{c.get('company_number')}](https://find-and-update.company-information.service.gov.uk/company/{c.get('company_number')})")
                if c.get('website'):
                    st.markdown(f"**Website:** {c.get('website')}")

            st.markdown("---")

            # The email itself, formatted properly
            subject = c.get('subject', '')
            body = c.get('body', '').replace("\\n", "\n")  # Handle literal \n if present

            email_text = f"Subject: {subject}\n\n{body}"

            st.markdown("**Generated Email**")
            st.code(email_text, language=None)

            if c.get("company_context"):
                with st.expander("Company context (Perplexity)"):
                    st.caption(c.get("company_context"))


if __name__ == "__main__":
    main()
