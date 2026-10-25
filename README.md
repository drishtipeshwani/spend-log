# Spend Log

Your family's card spends, tracked on your laptop.

A local-first tool that reads credit card statements from Gmail and shows a household where its money goes, across every card and every family member, without sending financial data to anyone else's server.

Built for the DEV Hacktoberfest Open-Source AI Challenge, Week 3: "Slow and Steady".

## What it does

Click **Fetch new statements** and Spend Log:

1. finds new statement emails in each family member's Gmail (read-only),
2. works out each PDF's password: **Gemma** reads the bank's hint in the email, and Python fills it in from the person's name and date of birth (whoever's details open it is the cardholder),
3. has Gemma read every transaction from the statement, whatever the bank's layout, while Python checks every amount, date and total against the PDF,
4. categorizes each merchant (once, remembered, correctable),
5. has **TabPFN** forecast each card's next statement, with Gemma explaining the biggest rises.

Then a Streamlit dashboard shows the whole family's spending by person, category, card and month, plus next month's forecast.

Everything runs on your laptop: Gemma through Ollama, TabPFN in local mode, data in CSV files. No statement, password or transaction leaves the machine.

| Part | Built with |
|---|---|
| Local model | Gemma 4 E4B via Ollama (JSON-schema outputs) |
| Forecasting | TabPFN (local, telemetry off) |
| Email | Gmail API, `gmail.readonly` |
| PDFs | pikepdf (unlock), pdfplumber (layout text) |
| Dashboard | Streamlit, Altair, pandas |

## Setup

### 1. Google Cloud (one time, ~20 min)

1. Go to https://console.cloud.google.com/ and create a project (e.g. `spend-log`).
2. **APIs & Services → Library**: search "Gmail API" and click **Enable**.
3. **APIs & Services → OAuth consent screen** (now called **Google Auth Platform**):
   - User type: **External**.
   - Fill in app name and your email.
   - **Data access / Scopes**: add `https://www.googleapis.com/auth/gmail.readonly`.
   - **Audience / Test users**: add your Gmail address (and each family member's, if they'll connect).
   - Leave the app in **Testing**. You'll see an "unverified app" warning on login; that's expected.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type: **Desktop app**.
   - Download the JSON and save it as `secrets/credentials.json` in this folder.

### 2. Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Spend Log uses a local model to read password hints, read transactions and categorize merchants. Install [Ollama](https://ollama.com) and pull Gemma:

```bash
ollama pull gemma4:e4b
```

### 3. List matching emails

```bash
python fetch_emails.py --subject "Credit Card Statement"
```

The first run opens a browser to log in and grant read-only access. The token is saved to `secrets/token_default.json` (file mode 600, gitignored).

Both scripts only look at the **last 30 days** by default. Use `--newer-than` to widen it.

More options:

```bash
# Only emails with a PDF, from the last 90 days
python fetch_emails.py --subject "Statement" --pdf --newer-than 90d

# Filter by sender
python fetch_emails.py --subject "Statement" --from hdfcbank.net

# Any raw Gmail search query
python fetch_emails.py --query 'from:(alerts@hdfcbank.net) has:attachment filename:pdf'

# Connect a second family member's account under its own name
python fetch_emails.py --account mom --subject "Statement"
```

### 4. Add your family's details

In the dashboard, **Family** shows each person with their Gmail status and how many cards and statements were found; **Add a person**: who (me, mom, dad), the name as printed on the card, date of birth, and optionally their Gmail. Or from a terminal (which also asks for an optional mobile number and card digits, used by a few banks' password rules):

```bash
python setup_profile.py
```

Run it once per person. It asks for the name as printed on the card and the date of birth, plus an optional mobile number and the last 4 digits of their cards. These are saved to `secrets/profiles.json`. `python setup_profile.py --list` shows who is set up.

### 5. Download, unlock and parse statements

```bash
python download_statements.py --subject "Credit Card Statement"
```

For each matching email this:

1. Saves the PDF as downloaded to `data/<account>/pdf/<date>_<gmail-id>_<filename>.pdf`.
2. Works out the password from the email (see below) and saves an unlocked copy to `data/<account>/unlocked/`.
3. Writes column-aligned text to `data/<account>/text/<same name>.txt`, with `--- page N ---` markers.
4. Writes `data/<account>/meta/<same name>.json`: whose card it is, card digits mentioned in the email, and how the password was found.
5. Parses new statements into the transaction CSVs (step 6).

Re-running skips emails it has already unlocked.

**Which emails.** Statement emails are found by subject, set in one line of `app/settings.py`:

```python
STATEMENT_SUBJECT = "Credit Card Statement"        # real bank statements
# STATEMENT_SUBJECT = "Demo Credit Card Statement" # demo emails with made-up statements
```

The real setting leaves out anything with "Demo Credit Card Statement" in the subject, so demo emails sitting in the same inbox are only picked up when you switch to the demo subject.

**Only new email.** Each fetch searches every connected inbox from that inbox's last fetch up to now. No new statement emails: nothing else happens. New ones are downloaded, unlocked and read into the CSVs. The time is saved only once everything found has been read, so a fetch that's interrupted, or a statement that can't be read or unlocked yet, is picked up by the next fetch. Times are kept per inbox and per subject setting in `data/<account>/sync.json`.

```bash
python download_statements.py   # new statements since last time
```

`--newer-than 90d` looks further back. A subject that hasn't been fetched yet starts from the newest statement on disk, or the last 30 days for a new account.

### 6. Build the transaction CSVs

`download_statements.py` runs this for you; run it by hand after editing categories. It works from the unlocked copies (no Gmail or passwords), and needs Ollama running.

```bash
python build_transactions.py                 # parse new statements, categorize new merchants
python build_transactions.py --force         # re-read every statement
python build_transactions.py --recategorize  # ask the model again, keeping your edits
```

It writes three files in `data/`:

| File | One row per | Columns |
|---|---|---|
| `transactions.csv` | transaction | txn_id, statement_id, holder, bank, card, card_last4, date, description, merchant, amount, direction (debit/credit), reward_points, category, category_source |
| `statements.csv` | statement | statement_id, account, holder, bank, card, card_last4, period_start, period_end, total_due, min_due, due_date, row_count, checks, pdf |
| `merchants.csv` | merchant | key, merchant, category, category_source |

**How a statement becomes rows.** Gemma reads each statement, whatever the bank:

1. pdfplumber turns each page into column-aligned text. Gemma reads that text, never the PDF itself.
2. One call reads the summary from page 1, with the email subject for the card name: bank, card, last 4 digits, statement period, due date, total and minimum due, and the printed totals for spends and payments.
3. One call per page returns the transactions in a fixed JSON shape (date, description, amount, debit/credit, reward points). Ollama forces the reply into that shape. Pages without any amounts are skipped.
4. Python checks every row: the amount must be printed on that page (and not used more often than it's printed), and the date must fall in the statement period. If a row fails, Gemma retries that page once, told what went wrong.
5. Debits and credits must add up to the totals printed on the statement. If they don't, pages with unaccounted-for amounts are retried with those amounts listed as a hint.

The result is in the `checks` column of `statements.csv` (`ok`, or what didn't add up). If Ollama isn't running, nothing is written for that statement and the next run tries again. A statement takes about a minute on a 16 GB Mac.

`tests/compare_extraction.py` measures Gemma against a hand-written reference parser for the AU Bank Ixigo statement (`tests/reference_au_ixigo.py`): currently 20/20 rows exact, with the summary matching too. Run it after changing prompts, or with `SPEND_LOG_MODEL=...` to compare models.

**Categories.** Python groups descriptions into merchant keys (`UPI/ZOMATO MEDIA PRIVA IND` → `ZOMATO MEDIA`). Gemma names and categorizes each new key once (`Zomato`, Food delivery), and the answer is saved in `merchants.csv`. Card payments are recognised by rule. To fix a category, edit it in `merchants.csv` and set `category_source` to `user`; it applies to every past and future transaction from that merchant, and `--recategorize` leaves it alone.

Categories: Food delivery, Cafes & restaurants, Groceries, Shopping, Travel, Cabs & transport, Fuel, Vehicle (service, repairs, FASTag, parking), Bills & utilities, Subscriptions (Netflix, Spotify, YouTube Premium, Prime...), Entertainment (movies, events), Health, Insurance, Education, Fees & charges, Transfers, Card payment, Refund, Other.

### 7. See where the money went

```bash
python spend_report.py                     # whole family, all months
python spend_report.py --holders mom       # one person
python spend_report.py --holders mom,dad   # a group
python spend_report.py --month 2026-09 --top 5
```

Totals by category, merchant, person, card and month. Spend is debits minus refunds; card payments are shown separately and not counted as spend.

### 8. Forecast the next statement

Runs automatically whenever a fetch reads new statements (nothing new, no new forecast). To run it by hand, e.g. after editing categories:

```bash
python forecast.py
```

For every card with 3+ statements, TabPFN predicts the next statement: a likely total with its range, and the spend per category. Every card's past statement cycles become one table, a row per card, cycle and category, with what was spent plus that card and category's spend in the three cycles before. TabPFN learns from all the rows at once, which is what makes six months of history enough, and returns the 10th, 50th and 90th percentile for the next cycle. Gemma then reads the forecast and suggests where to cut back. Results: `data/forecast.csv` and `data/forecast_tips.json`.

TabPFN runs locally. The first run opens a browser to sign in to Prior Labs and accept the (non-commercial) licence, then downloads the weights; after that it works offline, and no spending data is ever sent. Telemetry is switched off (`TABPFN_DISABLE_TELEMETRY=1`).

### 9. Dashboard

```bash
streamlit run dashboard.py
```

Opens at http://localhost:8501. Two pages, Overview and Forecast, with filters across the top: people as coloured pills (each person's colour is the same in every chart, so the pills are also the legend; pick one for a solo view or any group), a period (30 days, 2, 3 or 6 months, or a custom range), and a **Cards** button with a checklist grouped by person.

The look is a calm "sloth moss" palette (moss green, clay, sage on warm paper with white cards), set in `.streamlit/config.toml` and at the top of `dashboard.py`.

- **Overview**: spent and number of transactions in the period, where the money went by category, week by week or month by month (stacked by person), and spend by card and by person.
- **Forecast** (switch at the top): each card's likely next statement with its range, next month by category, and Gemma's tips on where to cut back. It's updated whenever a fetch reads new statements, and always learns from every statement fetched so far.
- **Where it went**: each bar says how much more (▲) or less (▼) that category cost than in the period before. **Click a category** to open it: its merchants, month by month, and every transaction in it.
- **Click a card** to open that card: its spend and where it went in the selected period, and its statements, newest first (the due date shows only on the latest, the one still to pay). Click a statement to see its transactions below. A statement whose transactions don't add up to its printed totals shows a warning.

**Several Gmail inboxes.** If Mom's statements arrive in her own Gmail, add her with that address in **Family** and click **Connect Gmail** next to her: a browser window opens to sign in as her (read-only). From then on, every fetch reads all connected inboxes. Each address must first be added as a test user in your Google Cloud project (Google Auth Platform → Audience → Test users). The simpler alternative: Mom sets a Gmail filter to forward statement emails to your inbox, and only yours needs connecting.

**Fetch new statements** (top right) checks every connected Gmail account for statements since the last fetch, then downloads, unlocks and reads only the new ones with Gemma, showing a live checklist per inbox (emails found, unlocked, read with Gemma, forecast updated). Afterwards a card lists the statements that just arrived, each with an **Open** button, and the full log is under Details. When new statements were read it then updates the forecast; when there's nothing new, nothing changes. Under the button it says when you last fetched. Which emails count as statements is set by their subject, in `app/settings.py` (see step 5). If Google's weekly sign-in has expired, a browser window opens to sign in again. If a password can't be worked out, that statement is left locked; run `python download_statements.py` in a terminal to type it in.

In any transactions table, change a category and save; it applies to every transaction from that merchant (saved in `merchants.csv` as your choice). Each view has its own URL, so you can bookmark one. To re-read statements already downloaded, run `python build_transactions.py`.

Each person keeps one chart colour everywhere (green, blue, clay, plum), checked to stay distinguishable for colour-blind readers.

The dashboard only listens on localhost and Streamlit's usage statistics are off (`.streamlit/config.toml`). To open it on your phone over home Wi-Fi, run it with `--server.address 0.0.0.0` and visit your laptop's IP on port 8501.

### Re-extract text without Gmail

```bash
python extract_statements.py --force
```

Rewrites the column-aligned text in `data/<account>/text/` from the unlocked copies. Handy for reading a statement the way Gemma sees it.

### How passwords are worked out

Banks put the password rule in the statement email, e.g. *"first 4 letters of your name in capitals followed by your date of birth in DDMM format"*.

1. **Find the hint.** The lines that mention the password are pulled out of the email body.
2. **Read it with a local model.** Gemma (via Ollama) turns the hint into a structured recipe, like `first 4 letters of name (upper) + DOB as DDMM`. The model only sees the bank's wording. It never sees anyone's name or date of birth.
3. **Read it with rules too.** Regexes catch the common phrasings, as a cross-check and as a fallback when Ollama isn't running.
4. **Fill in each family member's details.** Python applies the recipe to every profile, tries the results, and the one that opens the PDF tells you **whose card it is**. Card digits mentioned in the email ("card ending 1234") are used as well.
5. **Fallbacks.** If the email has no usable hint, common Indian card patterns are tried. If nothing works, you're asked to type the password (hidden, not saved). `--no-prompt` skips that.

Options: `--no-llm` reads hints with rules only. The model defaults to `gemma4:e4b`; set `SPEND_LOG_MODEL` to use another Ollama model.

## Privacy

- Scope is `gmail.readonly`. The app can't send, delete or modify mail.
- OAuth client and tokens stay in `secrets/` on this machine and are never committed.
- The download scripts print subject, sender, date and attachment names only, never email bodies, statement contents or passwords. `spend_report.py` shows your spending, since that's its job.
- Password hints and statement text go only to the local model on this machine. Family details (names, dates of birth) never go to the model.
- Downloaded and unlocked PDFs, extracted text, the transaction CSVs, and `secrets/profiles.json` are saved with file mode 600 and are gitignored (`data/`, `secrets/`).

## Known limitations

- In Google's Testing mode, refresh tokens expire after 7 days. The script falls back to a fresh browser login when that happens.
- Family details are stored in plain text (owner-readable only) for now. Encrypting them is on the list.
- Only the last 4 card digits are known, so rules that need other card digits can't be filled in.
- Transaction extraction is tested on a real AU Bank Ixigo statement (20/20 rows match a hand-written reference parser) and on 42 made-up statements in 7 different layouts (all add up to their printed totals). Other layouts should work; the `checks` column says when a statement doesn't add up.
- A card needs 3 or more statements before it's forecast.
- TabPFN's weights are under a non-commercial licence (accepted on first run); Spend Log's own code is MIT.
- Scanned statements (images with no text) aren't supported yet.

## License

MIT, see [LICENSE](LICENSE).
