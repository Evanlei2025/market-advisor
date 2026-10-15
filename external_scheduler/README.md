# Independent Scheduler

This Cloudflare Worker independently dispatches the daily publication, Saturday weekly publication, and daily heartbeat. Each dispatch carries its logical date, original Shanghai schedule slot, and stable trigger ID. It polls GitHub Actions until the matching run has a `run_started_at` timestamp and is `in_progress` or `completed`; a queued or missing run after two minutes raises an operator alert.

The GitHub workflows must be published before deploying this Worker. The scheduler uses the existing ServerChan operator channel for launch failures and does not use subscriber delivery settings.

## Configure and deploy

1. Create a Cloudflare Workers account and install Wrangler.
2. Create a GitHub fine-grained token for `Evanlei2025/market-advisor` with Actions read and write access. Keep it out of this repository and chat.
3. From this directory, authenticate Wrangler, then configure secrets:

   ```powershell
   npx wrangler secret put GITHUB_TOKEN
   npx wrangler secret put SC_KEY
   ```

4. Run `npm test`, then deploy with `npx wrangler deploy`.
5. Confirm the next daily run shows a `cf-daily-...` trigger ID and that the Worker log contains `workflow_started` with its GitHub run URL. Repeat the check for the heartbeat and Saturday weekly trigger.

Cloudflare cron times are UTC and map to 19:35 daily, 17:35 Saturday, and 22:17 heartbeat in Shanghai. The supplied `scheduled_for` is used to record scheduler delay inside the workflow. A successful GitHub dispatch HTTP response alone is not considered a successful start.
