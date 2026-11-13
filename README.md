# Home energy usage

Daily GitHub Action pulls monthly gas and electricity usage from the Octopus Energy API, merges it into `site/data.json`, and publishes `site/` with GitHub Pages.

## Setup (about 5 minutes)

1. Create a new GitHub repo and upload everything in this folder (keep the `.github/workflows` path).
2. **Settings → Secrets and variables → Actions → New repository secret**, add:
   - `OCTOPUS_API_KEY` – your `sk_live_…` key (Octopus account → Personal details → API access)
   - `OCTOPUS_ACCOUNT` – your account number, e.g. `A-1234ABCD`
3. **Settings → Pages → Source: GitHub Actions**.
4. **Actions → Update energy data and deploy site → Run workflow**. The first run backfills all history.

After that it runs daily on its own. Your site is at `https://<username>.github.io/<repo>/`.

## How old data is protected

- A month already in `data.json` is only replaced if it is within the last 3 months (recent months can be incomplete or revised). Older months are never overwritten, only added if missing.
- If Octopus ever stops returning old data (e.g. a meter is replaced), what you already saved stays.
- Every update is a git commit, so earlier versions of `data.json` are always recoverable.

## Settings

- `GAS_UNIT` in the workflow: `m3` (default, smart gas meters, converted at about 11.2 kWh/m³) or `kwh`.
- Change `REFRESH_MONTHS` in `fetch_octopus.py` to refresh more or fewer recent months.

## Privacy

`data.json` holds only monthly totals, with no account or meter numbers. On a free GitHub account the published page is public to anyone with the link.
