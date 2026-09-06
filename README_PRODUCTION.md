# File2Link Production V2

## Architecture

The CGNAT host runs one public Telegram bot: the orchestrator. It makes outbound HTTPS requests to each Railway worker, so the orchestrator does not need a public IP or inbound port forwarding.

Each worker is a separate Railway deployment with its own storage channel and public hostname. If you need separate 100 GB Railway network quotas, keep workers in separate Railway projects/accounts according to the quota that applies to your plan; Railway's current project documentation describes 100 GB outbound network bandwidth at the project level, not per service. Do not assume several services in one project multiply that quota.

## Security

- User handlers are `filters.private` only.
- Membership is checked before `/start` and before media processing.
- Workers with `WORKER_ONLY=1` do not register user-facing Telegram handlers.
- Control endpoints require `X-Control-Token`.
- File URLs use Fernet authenticated encryption. Telegram channel/message IDs are not visible in the URL.
- Link TTL is decoded from the authenticated token, not by parsing ciphertext.
- HTTP responses use `Cache-Control: no-store`.

## Traffic safety

The worker counts bytes actually written to the HTTP response and stores the monthly counter in SQLite. A request reserves its entire response length before streaming. Concurrent requests therefore cannot reserve more than the configured 95 GB ceiling.

The orchestrator independently persists worker totals. It also disables workers when their persisted total reaches the limit and synchronizes that total back to the worker.

### Important Railway billing detail

Railway currently documents 100 GB outbound network bandwidth at the project level and separately documents network-egress pricing. The application counter is a safety controller for File2Link HTTP body bytes; it is not a byte-for-byte replacement for Railway's billing meter because network overhead and other outbound traffic can exist.

For the strongest protection:

1. Put each quota-bearing worker in the Railway project/account whose quota you intend to use.
2. Set `TRAFFIC_LIMIT_GB=95`.
3. Set `TRAFFIC_RESET_DAY` to the actual start day of that Railway billing cycle (1-28), not blindly to calendar day 1 if your billing cycle differs.
4. Attach a Railway Volume and use `/data/traffic.db` on workers so the worker's own counter survives normal restarts/redeploys where the volume is retained.
5. Keep only one replica per worker unless you intentionally design shared accounting; the controller is designed around one worker process per configured worker.
6. Monitor Railway's own Metrics/Billing pages in parallel.

## Worker setup

Workers no longer require a dedicated Telegram Bot API token. A worker authenticates to Telegram as a normal Telegram user through MTProto. This keeps the existing direct `worker -> client` HTTP streaming path unchanged.

Required variables:

- `API_ID`
- `API_HASH`
- `BIN_CHANNEL`
- `BASE_URL`
- `LINK_SECRET`
- `CONTROL_TOKEN`
- `WORKER_ONLY=1`

For the Telegram user session, use **one** of these two methods:

1. `WORKER_SESSION_STRING` — recommended for Railway/ephemeral deployments.
2. A persistent Pyrogram session file using `WORKER_SESSION_NAME` and `WORKER_SESSION_WORKDIR=/data` on a Railway Volume.

### Generate a worker session

`API_ID` and `API_HASH` identify the Telegram application; they do not by themselves authorize a Telegram account. Telegram requires a user authorization flow with a phone number and verification code, and may require the account's 2FA password. citeturn0search0turn0search1

Run the included setup utility from a terminal:

```bash
python worker_auth.py
```

Optionally set `WORKER_PHONE_NUMBER=+1234567890` before running it. Pyrogram will request the Telegram login code and, when applicable, the 2FA password. After successful authorization it exports a session string with `export_session_string()`. citeturn1search0turn1search4

Put that generated value in the worker environment as `WORKER_SESSION_STRING`. Treat the session string as a secret: it represents an authorized Telegram session. Pyrogram documents session strings specifically for persisting authorized clients without depending on a session file. citeturn1search1

The Telegram user account used by the worker must have access to its storage channel.

## Orchestrator setup

Required variables:

- `API_ID`
- `API_HASH`
- `BOT_TOKEN`
- `WORKERS_JSON`
- `ADMIN_IDS`
- `REQUIRED_CHATS` (if membership is required)

The orchestrator bot must have access to every worker storage channel because it copies incoming messages directly into the selected channel with Telegram's copy operation. The worker's Telegram user account must also have access to its own storage channel because that same user session is used to read and stream the media.

## Domains

Give every worker its own hostname, for example:

- `files-01.example.com`
- `files-02.example.com`
- `files-03.example.com`

Set each worker's `BASE_URL` to its own hostname and use the same value in that worker's `WORKERS_JSON` entry.

Railway custom domains require the DNS records Railway provides; both the routing record and ownership verification record are required. Railway provisions TLS automatically after verification.

## Commands

User:

- `/start`
- send media in a private chat

Admin:

- `/traffic` — monthly controlled egress for every configured worker
- `/workers` — health, state, active downloads and traffic


## Telegram anti-flood protection

The orchestrator now spaces user-facing outbound messages instead of calling Telegram repeatedly with no pacing.

Variables:

- `MESSAGE_MIN_INTERVAL_SECONDS=1.5` — minimum spacing between replies in the same private chat.
- `GLOBAL_MESSAGE_MIN_INTERVAL_SECONDS=0.10` — small global spacing between outbound bot messages.
- Telegram `FloodWait` responses are handled by sleeping for the requested period and retrying.

These are application-level safeguards; Telegram's own flood limits remain authoritative.

## ShrinkMe monetization

The orchestrator can automatically pass every generated File2Link URL through ShrinkMe before sending it to the user.

Variables:

- `SHRINKME_ENABLED=1`
- `SHRINKME_API_KEY=<your token>`
- `SHRINKME_TIMEOUT_SECONDS=10`
- `SHRINKME_API_URL=https://shrinkme.io/api`

The API token is read only from the environment and is never embedded in the source code. If ShrinkMe is temporarily unavailable or returns an invalid response, the bot falls back to the original File2Link URL so a shortener outage does not take the bot offline.

Important: ShrinkMe's terms require the visitor to click the shortened URL themselves and prohibit fake/automated traffic, manipulation of views, automatic redirects and incentivized clicks. The bot therefore only **creates and presents** the shortened link; it does not open, click, iframe, redirect through, or artificially generate traffic for the ShrinkMe URL.

The public ShrinkMe service describes its model as earning from visits to shortened URLs and exposes an API for programmatic shortening. See the official site and terms before production use.

## Link expiration

The default link lifetime is 7 days and is controlled by `LINK_TTL_DAYS` in the worker and orchestrator `.env` files. Set `LINK_TTL_DAYS=0` to disable expiration. The timestamp is embedded in the authenticated File2Link token, so the real File2Link URL becomes unavailable after the configured lifetime. ShrinkMe receives that same expiring File2Link URL as its destination; therefore, after expiration, a previously generated ShrinkMe URL can no longer reach the file. ShrinkMe does not provide a verified API parameter here for independently expiring the hosted short URL itself, so expiration is enforced at the protected destination rather than by relying on an undocumented ShrinkMe feature.

`LINK_TTL_SECONDS` remains supported as a backward-compatible fallback when `LINK_TTL_DAYS` is absent.

## Link invalidation

A link remains valid for resumable/range requests. The worker merges successfully served byte ranges. Once the entire file has been served successfully, the token is permanently rejected in that process and the Telegram storage message is deleted. A subsequent request therefore receives 404.

## Deployment order

1. Create one Railway worker project/service per quota-bearing worker.
2. Attach a Volume to each worker and mount it at `/data`.
3. Put the worker bot in its storage channel as administrator.
4. Configure the worker variables and deploy.
5. Add a custom domain to each worker and set the DNS records shown by Railway.
6. Verify `https://worker-domain/health` returns `OK`.
7. Verify `https://worker-domain/control/status` only works with the control token.
8. Configure `WORKERS_JSON` on the CGNAT host.
9. Put the orchestrator bot in every worker storage channel as administrator.
10. Configure required membership chats on the orchestrator and ensure it can query membership.
11. Start the orchestrator.
12. Test `/start`, group silence, membership denial, small file, HEAD, Range/resume, full download, link invalidation, `/traffic`, worker disable at the configured threshold, and worker restart.

## Operational warning

No application-level meter can promise that Railway's billing meter will stop at exactly 95,000,000,000 bytes. This implementation deliberately reserves complete HTTP responses so File2Link itself cannot exceed its configured cap, and it leaves 5 GB of headroom versus a nominal 100 GB project allowance. Railway's own network metrics/billing remain authoritative.
