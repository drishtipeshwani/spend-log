# Spend Log 🦥

Your family's credit card spends, sorted on your laptop while you do nothing.

## Overview

Spend Log reads credit card statements from each family member's Gmail and shows the whole household where its money goes: by person, category, card and month, plus a forecast of next month's bills.

Click **Fetch new statements** and it:

1. finds new statement emails in every connected inbox (read-only),
2. works out each PDF's password: **Gemma** reads the bank's hint in the email, and Python fills it in from the person's name and date of birth,
3. has Gemma read every transaction from the statement, whatever the bank's layout, while Python checks each amount and total against the PDF,
4. categorizes each merchant (once, remembered, and correctable in the dashboard),
5. has **TabPFN** forecast each card's next statement, with Gemma explaining the biggest rises.

Everything runs locally. Gemma runs through Ollama, TabPFN runs in local mode, and data is kept in CSV files on your machine. No statement, password or transaction is sent anywhere.

Built for the [DEV Hacktoberfest Open-Source AI Challenge, Week 3: "Slow and Steady"](https://dev.to/challenges/hacktoberfest-week3-2026-10-19). The write-up has the full story and design.

## Tech stack

| Part | What's used |
|---|---|
| Local model | [Gemma 4 E4B](https://ollama.com/library/gemma4) via [Ollama](https://ollama.com), with JSON-schema outputs |
| Forecasting | [TabPFN](https://github.com/PriorLabs/TabPFN), local mode, telemetry off |
| Email | Gmail API (`gmail.readonly`), Google OAuth for desktop apps |
| PDFs | `pikepdf` to unlock, `pdfplumber` for layout-preserving text |
| Data | pandas, plain CSV files |
| Dashboard | Streamlit, Altair |
| Language | Python |

## Run it locally

### What you need

- macOS or Linux with Python 3.11+ (developed on 3.14), 16 GB of RAM, and about 11 GB of free disk for the models (Gemma 9.5 GB, TabPFN 0.85 GB)
- [Ollama](https://ollama.com)
- A Google account, plus a Google Cloud project for Gmail access (free, one-time, about 15 minutes)

### 1. Get the code

```bash
git clone https://github.com/drishtipeshwani/spend-log.git
cd spend-log
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Get Gemma

```bash
ollama pull gemma4:e4b
```

Keep Ollama running while you use Spend Log. To try another model, set `SPEND_LOG_MODEL` (e.g. `SPEND_LOG_MODEL=gemma4:12b`).

### 3. Let Spend Log read Gmail

1. In the [Google Cloud console](https://console.cloud.google.com/), create a project and enable the **Gmail API** (APIs & Services → Library).
2. Open **Google Auth Platform** (the OAuth consent screen):
   - **Audience:** choose **External**, keep the app in **Testing**, and add every Gmail address you'll connect as a **test user**.
   - **Data access:** add the scope `https://www.googleapis.com/auth/gmail.readonly`.
3. Under **Clients**, create an **OAuth client ID** of type **Desktop app**. Download the JSON and save it as `secrets/credentials.json`.

Google shows an "unverified app" warning when you sign in. That's expected for an app in Testing. In Testing mode Google also expires the sign-in after 7 days, and Spend Log then opens a browser to sign in again.

### 4. Tell it which emails are statements

Statement emails are found by subject. Set the text your banks use in `app/settings.py`:

```python
STATEMENT_SUBJECT = "Credit Card Statement"
```

The repo ships with `"Demo Credit Card Statement"`, the subject of the made-up statements used in the demo video.

### 5. Start the dashboard

```bash
streamlit run dashboard.py
```

It opens at http://localhost:8501 and only listens on your machine.

1. **Family → Add a person**: who they are (me, mom, dad), the name as printed on their card, their date of birth, and their Gmail if statements arrive in their own inbox. Then click **Connect** to sign in to that inbox, read-only.
2. Click **Fetch new statements**. Each statement takes about a minute for Gemma to read on a 16 GB laptop. The first fetch looks back 30 days; later fetches only look at email since the last one.
3. The **Forecast** page fills in once a card has 3 or more statements. The first forecast opens a browser to accept TabPFN's licence and downloads its weights (about 850 MB), once. After that it works offline.

To load more history on the first run, fetch from a terminal with a longer window, once per inbox:

```bash
python download_statements.py --account mom --newer-than 180d
```

### Where things are kept

- `secrets/`: the OAuth client, Gmail sign-ins, and family details (names and dates of birth, used only to work out passwords).
- `data/`: downloaded and unlocked PDFs, extracted text, and the CSVs (`transactions.csv`, `statements.csv`, `merchants.csv`, `forecast.csv`).

Both folders are gitignored, and files are written readable by you only.

### Command-line tools

| Command | What it does |
|---|---|
| `python download_statements.py` | Same as the Fetch button; `--newer-than 90d` looks further back |
| `python build_transactions.py` | Re-read downloaded statements (`--force` for all, `--recategorize` to redo categories) |
| `python forecast.py` | Re-run the forecast, e.g. after fixing categories |
| `python spend_report.py --holders mom` | Spending report in the terminal, for one person or a group |
| `python setup_profile.py` | Add a person from the terminal, with optional mobile and card digits (some banks' password rules use them) |
| `python tests/compare_extraction.py` | Check Gemma's extraction against a hand-written parser for one bank's layout |

## License

MIT, see [LICENSE](LICENSE). TabPFN's model weights have their own non-commercial licence, which you accept on first use.
