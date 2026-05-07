# ConstructReach

UK construction outbound pipeline. Built as Section 7 of the Lumina Head of Outreach screening submission.

## Pipeline

1. **Discover companies** — Perplexity finds top UK general contractors by reputation (Balfour Beatty, Skanska, Kier, Laing O'Rourke, Mace, etc.)
2. **Identify decision-makers** — Companies House API lookup by company name, then officers endpoint to find current directors
3. **Verify emails** — Prospeo enrich-person endpoint with first name, last name, company website
4. **Pull company context** — Perplexity for recent projects, contract wins, ESG commitments
5. **Generate personalised outreach** — Anthropic Claude Haiku 4.5 with full company + person context

Each company runs through all five stages. Results land in a SQLite database and render in a Streamlit UI with the dark Gotham aesthetic.

## Why Perplexity for company discovery, not Companies House SIC search

Companies House SIC code search returns thousands of incorporated entities — many of them are dormant, shell, or too small to appear in Prospeo's database. Perplexity surfaces the *actual* top UK general contractors by reputation, which Prospeo definitely has data on.

We still use Companies House for the second step (officer lookup) because it's the authoritative source for UK director records.

## Stack

- Streamlit (frontend + Python runtime)
- SQLite (state, audit log)
- Direct API calls (no orchestration framework)
- Anthropic, Perplexity, Prospeo, Companies House APIs

## Running locally

```bash
pip install -r requirements.txt
cp .env.example .env  # then fill in API keys
streamlit run app.py
```

## Deploy to Streamlit Cloud

1. Push this repo to GitHub
2. Connect at [streamlit.io/cloud](https://streamlit.io/cloud)
3. Add your API keys as secrets in the Streamlit Cloud dashboard

## What's deferred for production

- Multi-domain email sending infrastructure (covered in question 12)
- Reply handling and classification agent
- Feedback loop tagging successful outreach
- Tier-based channel mix (email / LinkedIn / voice / cold call)
- Scale to 10,000+ companies with the supervisor and governance layers
