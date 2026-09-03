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

Required variables:

- `API_ID`
- `API_HASH`
- `BOT_TOKEN`
- `BIN_CHANNEL`
- `BASE_URL`
- `LINK_SECRET`
- `CONTROL_TOKEN`

Recommended:

- `WORKER_ONLY=1`
- `TRAFFIC_LIMIT_GB=95`
- `TRAFFIC_DB_PATH=/data/traffic.db`
- `MAX_CONCURRENT_DOWNLOADS=2`
- `LINK_TTL_SECONDS=0`
- `REQUIRED_CHATS=`

The worker bot must have access to its storage channel, normally as administrator.

## Orchestrator setup

Required variables:

- `API_ID`
- `API_HASH`
- `BOT_TOKEN`
- `WORKERS_JSON`
- `ADMIN_IDS`
- `REQUIRED_CHATS` (if membership is required)

The orchestrator bot must have access to every worker storage channel because it copies incoming messages directly into the selected channel with Telegram's copy operation.

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
