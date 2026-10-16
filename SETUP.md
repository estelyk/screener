# Putting the screener online (one-time setup, ~20 minutes)

After this, GitHub runs the screener every weekday evening and updates your
passphrase-locked page. Your Mac can be off.

## 1. GitHub account
1. Sign up at github.com. Your username appears in the page address, so pick one
   you're comfortable with.
2. Settings → **Emails** → turn on **Keep my email addresses private** and
   **Block command line pushes that expose my email**.

## 2. Upload the code with GitHub Desktop
GitHub Desktop handles sign-in and automatically skips private files
(`.venv`, `data`, `output`, `site`, `my_portfolio.json`).

1. Install GitHub Desktop from desktop.github.com and sign in.
2. GitHub Desktop → Settings → **Git** → Email: choose the one ending in
   `@users.noreply.github.com`.
3. File → **Add Local Repository** → choose `Desktop/projects/buffett-screener`.
   It will say it isn't a repository yet → click **create a repository** →
   **Create Repository**.
4. Check the **Changes** list. It should show only code files: `.py` files,
   `site_template.html`, `config.py`, `README.md`, `SETUP.md`, `requirements.txt`,
   `assets/`, `tests/`, `.github/`, `.gitignore`, `my_portfolio.example.json`.
   **If you see `.venv`, `data`, `output`, `site`, or `my_portfolio.json`, stop and
   tell Claude.**
5. Click **Publish repository**. Pick a neutral name (e.g. `screener`), and
   **uncheck "Keep this code private"** (free GitHub Pages needs a public repo;
   your results stay encrypted).

## 3. Add your two secrets
On github.com, open the repository → **Settings** → **Secrets and variables** →
**Actions** → **New repository secret**, twice:

| Name | Value |
|---|---|
| `SEC_USER_AGENT` | `Kyle Tse your-email@example.com` (SEC requires a name and email) |
| `PAGE_PASSPHRASE` | your passphrase, e.g. four or five random words |

Secrets are hidden from everyone, including in the logs. Never put the
passphrase anywhere else in the repository.

Optional third secret, for holding-review emails:

| Name | Value |
|---|---|
| `HOLDINGS` | the tickers you own, comma-separated, e.g. `KO, COST` |

Update it whenever you buy or sell (open the secret → **Update**). Also enter the
same tickers in the app under **Sizing → Your holdings** so the flags show there.

## 4. Turn on GitHub Pages
Repository → **Settings** → **Pages** → under *Build and deployment*, Source:
**GitHub Actions**.

## 5. First run
Repository → **Actions** tab → **Daily screen** → **Run workflow** → **Run workflow**.
The first run takes about 10–15 minutes (it downloads all the SEC data). Later
runs are faster. A green check means it worked.

## 6. Open it on your phone
1. The address is `https://YOUR-USERNAME.github.io/REPO-NAME/`
   (also shown on the Settings → Pages screen).
2. Open it in Safari, enter your passphrase, keep **Remember on this device** checked.
3. Share button → **Add to Home Screen**.
4. Tap **Sizing** to enter your cash reserve and limits (stored only on the phone).

## What happens from now on
- **Weekdays ~5–6pm ET:** GitHub runs the screener and updates the page.
- **New buy signal:** GitHub opens an issue titled "New buy signal" and emails you.
  It never names the stock; open your page to see it.
- **A holding gets a new review flag** (fails the quality screen, a "cheap for a
  reason" warning, or price more than 50% above estimated value): GitHub opens a
  "Holding needs a review" issue and emails you, without naming the stock.
- **A run fails** (e.g. Yahoo blocks prices): GitHub emails you, and yesterday's
  page stays up. Occasional failures are normal; repeated ones, tell Claude.
- **Inactivity:** GitHub pauses schedules after 60 days without repository
  activity and emails you first. Click the link in that email to re-enable.

## Changing something later
Edit files in the folder on your Mac (e.g. `config.py`), then in GitHub Desktop:
write a short summary → **Commit to main** → **Push origin**. The next run uses it.

## If you ever want it gone
Repository → Settings → (bottom) **Delete this repository**. The page goes with it.
