# Moodle Robot dashboard

A single page that shows what's new in Moodle, built from the reports the Moodle Robot emails you.

Each robot run sends a `[moodle-robot] …` email with a `changes-<run>.json` attachment.
`sync.py` reads those emails, along with the `JKU Moodle: …` notifications in the same inbox.
It throws away the noise and writes `data.js`. `index.html` turns that into the dashboard.

What you see:

- **Next deadline**: a live countdown, with how much of the submission window has passed.
- **What's new**: grades, new assignments, new material and forum posts from the last 7 days, grouped by day and filterable by type.
- **Up next**: every open to-do with a deadline, plus past-due and no-deadline items.
- **Fresh grades** and **From your inbox**, which shows Moodle's own emails, with sign-in alerts grouped.
- **Charts**: activity per day, a deadline runway, the busiest courses, and robot runs (kept vs. filtered). Each chart has a table view.

What counts as noise: checkmark row-hash churn, empty grade placeholders (`- Grade analysis`), Zoom/Opencast links whose date rolls forward each week, a date or status that flips and flips back, and duplicates. A new assignment is reported both as "item added" and as "new action", and a grade as "grade changed" and as "graded". In a typical week this cuts about 200 raw changes down to about 55.

## Run it

You need Python 3.9+. There's nothing to install.

1. Create a Gmail **app password**: Google Account → Security → 2-Step Verification → App passwords.
2. Sync:

   ```sh
   export GMAIL_USER=you@gmail.com
   export GMAIL_APP_PASSWORD='abcd efgh ijkl mnop'
   python3 dashboard/sync.py --name Omar
   ```

3. Open `dashboard/index.html` in a browser. Re-run step 2 whenever you want fresh data. The page itself refreshes relative times every minute.

The script only reads. It uses `BODY.PEEK`, so nothing gets marked as read. Downloaded attachments are cached in `dashboard/reports/`.

### Other options

```sh
# Build from attachments you saved by hand (no network)
python3 dashboard/sync.py --reports ~/Downloads/robot-reports

# One self-contained HTML file with the data baked in, for sharing or recording
python3 dashboard/sync.py --name Omar --standalone dashboard/out/moodle-dashboard.html

# Different window, time zone, or mail filters
python3 dashboard/sync.py --days 14 --tz Europe/Vienna \
  --robot-query 'subject:"[moodle-robot]"' --inbox-query 'from:noreply@jku.at subject:"JKU Moodle"'
```

`data.js`, `reports/` and `out/` are git-ignored, so your data never gets committed.

## Put it on Vercel

On Vercel there is no `data.js`. The page calls `/api/data`, a Python function in `api/data.py` that reads Gmail live and caches the result for 10 minutes.

1. Go to [vercel.com/new](https://vercel.com/new) and import this GitHub repo. Keep the defaults: no framework and no build command. `vercel.json` handles the rest.
2. Under **Settings → Environment Variables**, add:

   | Name | Value |
   |---|---|
   | `GMAIL_USER` | the Gmail address the robot reports go to |
   | `GMAIL_APP_PASSWORD` | the app password from above |
   | `DASHBOARD_KEY` | a long random string, e.g. from `openssl rand -hex 16` |
   | `MOODLE_DASH_NAME` | *(optional)* your first name |

3. **Redeploy** so the variables take effect, then open the site and enter the key once. You can also share `https://your-app.vercel.app/#key=YOUR_KEY`. The key is saved on that device and removed from the address bar.

Safety defaults:

- Nothing is served until `DASHBOARD_KEY` is set.
- Responses are `no-store` and `noindex`.
- Other students' names and post text are removed on the server. Set `SHOW_STUDENT_NAMES=1` if you want them back on your private deployment.
- Every push to GitHub redeploys automatically.

Optional variables: `DASHBOARD_DAYS` (default 7), `DASHBOARD_TZ` (default Europe/Vienna), `DASHBOARD_TTL` in seconds (default 600), `ROBOT_QUERY`, `INBOX_QUERY`.

## Recording a video

- **Privacy mode is on by default.** Other students' names become "A student" and their post text is hidden. Staff posts and announcements stay visible. Press **P** to toggle it. The "Privacy on" pill in the header tells you which mode you're in.
- Press **T** to switch between dark and light.
- URL parameters for repeatable takes: `index.html?theme=dark&privacy=1&now=2026-11-26T09:30`. `now` freezes the starting time, and the countdown still ticks from there.
- 1440×900 or 1920×1080 fit the header, countdown, KPI tiles and the top of the feed on the first screen. The cards animate in on load, and the animation is skipped if the OS asks for reduced motion.

## Tests

```sh
python3 -m unittest discover dashboard
```
