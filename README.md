# Hedge Fund Tracker

A dashboard that shows what 16 well-known investors are buying and selling, using their public SEC filings, with prices and returns in £ and $, Trading 212 availability hints, and optional WhatsApp alerts.

> **Research tool, not financial advice.** 13F filings appear up to 45 days after each quarter ends, so the moves you see are 6 to 20 weeks old and may already have been reversed.

## What it tracks

| Signal | Fund | Manager |
|---|---|---|
| High | Berkshire Hathaway | Buffett / Abel |
| High | Duquesne Family Office | Stanley Druckenmiller |
| High | Pershing Square | Bill Ackman |
| High | Baupost Group | Seth Klarman |
| High | Himalaya Capital | Li Lu |
| High | Akre Capital | Chuck Akre / John Neff |
| Medium | Appaloosa | David Tepper |
| Medium | Fundsmith | Terry Smith |
| Medium | Third Point | Dan Loeb |
| Medium | Viking Global | Andreas Halvorsen |
| Medium | Lone Pine Capital | Mandel's firm |
| Medium | Elliott | Paul Singer |
| Low | Scion | Michael Burry |
| Low | Point72 | Steve Cohen |
| Low | Renaissance Technologies | quant |
| Low | Bridgewater | Dalio's firm |

To add or remove a fund, edit `config.json` (you need the fund's SEC "CIK" number, which you can find by searching the fund name at sec.gov/edgar). If a fund moved its filings to a new SEC filer, put the new number in `cik` and the old one in `also_ciks`, as Pershing Square does.

## Option 1: run it on your Mac

1. Double-click **`Update and open dashboard.command`**.
   (The first time, macOS may block it: right-click it, choose **Open**, then **Open** again.)
2. The first run takes 10 to 15 minutes while it downloads filings and looks up tickers. After that it takes a minute or two.
3. The dashboard opens in your browser. Run it again whenever you want fresh data.

If you'd rather use Terminal: `cd` into this folder and run `python3 tracker.py`, then open `docs/index.html`.

## Option 2: put it online, updating itself for free (recommended)

GitHub can run the updater twice a day and host the dashboard as a web page you can open on your phone.

1. Create a free account at github.com (if you haven't got one) and create a new **public** repository, e.g. `hedge-fund-tracker`. (Free accounts can only host pages from public repos. Nothing personal is in the code; your WhatsApp keys go in hidden "secrets".)
2. Upload all the files in this folder (drag and drop on the repo page works). Include the `.github` folder.
3. In the repo: **Settings → Pages → Build and deployment → Deploy from a branch → main / docs → Save.**
4. In the repo: **Actions** tab → enable workflows → **Update tracker** → **Run workflow**.
5. After about 15 minutes your dashboard is at `https://<your-github-name>.github.io/hedge-fund-tracker/`.

## WhatsApp alerts

You get a WhatsApp message when a tracked fund files a new 13F with big moves (new positions or exits of 0.5%+ of the fund, or any trade of 1%+), when 3+ funds buy the same stock, when a fund files a large Form 4 trade (over $5m), or when a fund files a new activist 13D stake. The first run only records a starting point, so you won't get a flood of old news.

**Option A: CallMeBot (free, 2 minutes)**
1. Go to callmebot.com → WhatsApp, save the bot's number in your contacts, and send it the activation message they show.
2. It replies with an API key.

**Option B: Twilio (more reliable, pay as you go)**
1. Sign up at twilio.com, open Messaging → Try it out → WhatsApp sandbox, and join the sandbox from your phone.
2. Note your Account SID, Auth Token and the sandbox number.

**Then tell the tracker:**
- On your Mac: copy `alerts.env.example` to `alerts.env` and fill it in. Test with `python3 tracker.py --test-alert`.
- On GitHub: repo **Settings → Secrets and variables → Actions → New repository secret**, and add `CALLMEBOT_PHONE` and `CALLMEBOT_APIKEY` (or the four `TWILIO_...` values).

Alert thresholds live in `config.json` under `"alerts"`.

## Where the data comes from

- **SEC EDGAR**: 13F holdings, Form 4 insider trades, Schedule 13D/13G stakes (free, official).
- **OpenFIGI**: turns the CUSIP codes in filings into tickers (free; an optional free API key makes the first run faster).
- **Yahoo Finance**: share prices and the GBP/USD rate (free, unofficial).

## Ideas for later

- Connect your Trading 212 API key so the "on Trading 212" badge is exact rather than a guess.
- Add a weekly email digest.
- Track UK-listed UCITS ETF equivalents for the US ETFs Bridgewater buys.
