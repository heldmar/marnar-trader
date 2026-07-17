# Installing MarNar Trader on your own server

This guide takes you from `git clone` to your own running instance in **paper
mode** — the system trades simulated money on live Binance market prices, so
you can watch it work with zero risk. It runs on any machine with Docker
(x86-64 or ARM; a Raspberry Pi 4 is enough).

No exchange account or API keys are needed for paper mode. Market data is
public; the "account" is a crash-safe local simulation.

## 1. Prerequisites

- Docker with the Compose plugin (`docker compose version` works).
- ~1 GB of free RAM and a few GB of disk for market data.
- Outbound internet access (Binance public API, Telegram, GDELT news).

> **Note:** Binance blocks its API from some jurisdictions (including US
> cloud-datacenter IPs). Paper mode needs to reach `api.binance.com`.

## 2. Clone

```sh
git clone <your-fork-or-this-repo-url> marnar-trader
cd marnar-trader
```

## 3. Create your own Telegram bot (recommended)

The system pushes trade alerts and a daily plain-language report to Telegram.
It works without this (reports still land in the web UI), but you'll want it.

1. In Telegram, open a chat with **@BotFather**.
2. Send `/newbot`. Give it a display name and a unique username ending in
   `bot` (e.g. `my_trader_bot`). BotFather replies with an **HTTP API
   token** like `1234567890:AAAbbbCCC...` — that is your
   `TELEGRAM_BOT_TOKEN`. Treat it like a password.
3. Open a chat with your new bot (search for its username) and send it any
   message — e.g. "hello". Bots cannot message you first.
4. Get your **chat id**: open
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and
   find `"chat":{"id":123456789,...}` in the response. That number is your
   `TELEGRAM_CHAT_ID`.

## 4. Configure secrets (environment only — never files in Git)

Create a `.env` file next to `docker-compose.yml` (it is gitignored):

```sh
TELEGRAM_BOT_TOKEN=1234567890:AAAbbbCCC...
TELEGRAM_CHAT_ID=123456789
```

That's the complete secret inventory for paper mode. If you deploy through a
UI like Portainer instead, set these as stack environment variables — same
rule: secrets live in the environment, never in tracked files.

## 5. Start the stack

```sh
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build
```

This builds and starts two containers:

- `marnar-trader-core` — the trading service (never exposed publicly).
- `marnar-trader-ui` — the web UI on `http://localhost:18081`, which proxies
  `/api` to the core over the internal Docker network.

## 6. Switch it to paper mode

The service defaults to a do-nothing mode until told what to run. Write the
one-line config onto the data volume and restart:

```sh
docker compose -f docker-compose.yml -f docker-compose.local.yml \
  exec core sh -c 'echo "mode: paper" > /data/config.yaml'
docker compose -f docker-compose.yml -f docker-compose.local.yml restart core
```

Everything else has sensible defaults (20-pair universe, Donchian 15/15
strategy on daily candles, 3% protective stops, $150 simulated capital, risk
circuit breakers on). All of it is editable later — in the UI's Settings tab
or in `/data/config.yaml`.

## 7. First boot — what to expect

- Open **http://localhost:18081**. You should see the amber **PRACTICE
  MODE** banner and "Trading normally".
- Telegram receives a startup message within a minute
  (`📈 MarNar paper engine up: ...`). If it doesn't, re-check token/chat id
  in `.env` and `docker compose ... logs core | grep -i telegram`.
- The engine trades on **daily** candles — days or weeks with no trades are
  normal and correct. The Activity tab shows every decision as it happens.
- After the first full UTC day, the first **daily report** arrives on
  Telegram and appears in the UI's Reports tab (a weekly summary follows
  every Monday). Reports are generated for every completed day since the run
  started, even across downtime.
- Don't want the Telegram copies? Toggle them off in Settings → "Telegram
  reports", or send the bot `/reports_off` (and `/reports_on` to resume).
  Reports always remain available in the web UI archive.

Health check from the host: `curl http://localhost:18081/api/health`.

## 8. Exposing it beyond localhost (optional)

The shipped `docker-compose.yml` is the reference production stack; adapt it
to your environment:

- Replace the data volume with a bind mount on real storage
  (e.g. `/srv/marnar-trader:/data`) and the network with whatever your
  reverse proxy uses.
- Route your reverse proxy (Nginx Proxy Manager, Caddy, Traefik — anything)
  to the **ui** container, port 80. The UI serves the API same-origin, so a
  single route suffices.
- **Put authentication in front of it** (an access list, basic auth, or your
  SSO). The UI has a kill switch and a settings editor — it must not be
  reachable anonymously.
- **Never route the core container publicly.** Only the UI proxies to it.

## 9. Operating, updating, resetting

- **Operating:** see [RUNBOOK-PAPER.md](RUNBOOK-PAPER.md) — health checks,
  kill switch, the weekly ops checklist, and crash/restart semantics.
- **Updating:** `git pull`, then rebuild with the same `up -d --build`
  command. Restarts never lose state and never reset the paper-run clock —
  the journal on the data volume is the system of record.
- **Resetting the paper run:** stop the stack and delete the data volume
  (`docker volume rm marnar-trader_trader-data-local`). Everything —
  journal, simulated account, candles, reports — starts fresh.

## 10. Going live (read this before even thinking about it)

Live trading is deliberately gated. Before enabling it:

- Complete a multi-week paper run and review the parity report
  (`python -m trader.parity` inside the core container).
- Create Binance API keys that are **trade-only — withdrawal permission
  disabled** — and provide them as environment variables, nowhere else.
- Start with money you can afford to lose entirely. The strategy is
  evidence-based, not a promise.
