# signal-cli-gateway

**Script any Signal account from any language.** A small REST + WebSocket
gateway (and Python SDK) on top of [`signal-cli`](https://github.com/AsamK/signal-cli),
the community client built on Signal's official `libsignal` library.

This project does **not** modify signal-cli. It talks to the signal-cli
daemon over JSON-RPC and exposes HTTP, WebSocket, and a Python SDK — so group
and account operations (create/join groups, add or remove members, send
messages) are easier than raw RPC. It is **not affiliated** with Signal
Messenger or the signal-cli project. signal-cli is GPL-3.0; this repository
is MIT.

> Signal has **no official Bot API and no public user-account API** (nothing
> like Telegram's `api_id`/`api_hash`/Telethon). `signal-cli` is the de-facto
> standard way to automate an account, but it speaks newline-delimited
> JSON-RPC over a socket. This project turns that into an API anyone can call
> from `curl`, Python, Node, a cron job or a webhook.

```
your script ──REST/WS──▶ signal-gateway ──JSON-RPC/TCP──▶ signal-cli daemon ──▶ Signal servers
   (any lang)              (this project)                  (multi-account)
```

## Features

- **Multi-account**: one gateway drives every account registered/linked in
  signal-cli; each request names its `account` (`+39...` number or ACI UUID)
- **Two onboarding flows**
  - *Link* (recommended): scan a QR code, gateway becomes a linked device of
    your phone — no SMS, no captcha
  - *Register*: dedicate a phone number (SMS/voice code, captcha when Signal
    asks for it)
- **Send** text/attachments/quotes, **reactions**, **groups**, **contacts**,
  block/unblock — with a typed REST API plus a `/v1/rpc` escape hatch for
  every other signal-cli method (`sendPollCreate`, `updateProfile`, `trust`, …)
- **Receive in real time** via WebSocket (`/v1/events`) and/or signed webhooks
- **Built-in anti-abuse throttling**: sends per account are serialised with a
  minimum interval and a per-minute cap (configurable, conservative by default)
- **Python SDK** + ready-to-run examples, Swagger docs at `/docs`

## Quick start (Docker)

```bash
cp .env.example .env        # set SG_API_KEY to a long random string
docker compose up -d --build
curl http://localhost:8000/health
```

### 1a. Link your phone (recommended)

```bash
python examples/link_account.py   # writes link-qr.png, scan it from the app:
                                  # Signal -> Settings -> Linked devices -> +
curl -H "X-API-Key: $SG_API_KEY" http://localhost:8000/v1/accounts
```

### 1b. …or register a dedicated number

```bash
# If Signal demands a captcha: open
# https://signalcaptchas.org/registration/generate.html , solve it, copy the
# "Open Signal" link (signalcaptcha://...) and pass it as "captcha".
curl -X POST -H "X-API-Key: $SG_API_KEY" -H 'Content-Type: application/json' \
  http://localhost:8000/v1/accounts/register \
  -d '{"account":"+393331234567","captcha":"signalcaptcha://..."}'

curl -X POST -H "X-API-Key: $SG_API_KEY" -H 'Content-Type: application/json' \
  http://localhost:8000/v1/accounts/+393331234567/verify -d '{"code":"123456"}'
```

### 2. Send and receive

```bash
curl -X POST -H "X-API-Key: $SG_API_KEY" -H 'Content-Type: application/json' \
  http://localhost:8000/v1/messages/send \
  -d '{"account":"+393331234567","recipients":["+393339876543"],"message":"ciao!"}'
```

```python
from client.sdk import SignalGateway

sg = SignalGateway("http://localhost:8000", api_key="...")
sg.send("+393331234567", "ciao!", recipients=["+393339876543"])

async for event in sg.events(account="+393331234567"):   # incoming messages
    print(event["source"], event["text"])
```

Run a bot in ~20 lines: [`examples/echo_bot.py`](examples/echo_bot.py).

## Without Docker

```bash
signal-cli daemon --tcp 127.0.0.1:7583 --receive-mode on-connection &
pip install .
SG_API_KEY=... signal-gateway        # http://localhost:8000/docs
```

signal-cli must be **kept up to date**: Signal retires protocol versions and
clients older than ~3 months may stop working.

## API overview

| Method | Path | Description |
|---|---|---|
| GET | `/health` | gateway + daemon status |
| GET | `/v1/accounts` | list accounts |
| POST | `/v1/accounts/link/start` | start QR linking (returns QR PNG, base64) |
| POST | `/v1/accounts/link/finish` | complete linking after the scan |
| POST | `/v1/accounts/register` | register a number (SMS/voice, captcha) |
| POST | `/v1/accounts/{account}/verify` | confirm registration code |
| POST | `/v1/messages/send` | send text/attachments (throttled, per account) |
| POST | `/v1/messages/reaction` | send/remove a reaction |
| GET | `/v1/contacts?account=` | list contacts |
| POST | `/v1/contacts/{c}/block\|unblock?account=` | block / unblock |
| GET/POST | `/v1/groups?account=` | list / create groups |
| PATCH | `/v1/groups/{id}` | rename, add/remove members |
| WS | `/v1/events?account=&api_key=` | live incoming messages |
| POST | `/v1/rpc` | any signal-cli JSON-RPC method, typed-endpoint-free |

All `/v1/*` REST calls require header `X-API-Key: $SG_API_KEY` (when set).

Incoming-event shape (WebSocket & webhook):

```json
{"type":"message","account":"+39333...","source":"+39333...","source_name":"Mario",
 "timestamp":1730000000000,"is_sync":false,"group_id":null,"text":"ciao",
 "attachments":[],"mentions":[],"quote":null,"reaction":null,"raw":{...}}
```

Webhooks are POSTed to `SG_WEBHOOK_URL` with header
`X-SG-Signature: sha256=<hmac>` when `SG_WEBHOOK_SECRET` is set.

## ⚠️ Responsible use

This is a gateway for **your own account**, not a bulk-messaging tool:

- Signal rate-limits and **bans numbers** that send unsolicited or bulk-like
  traffic — much faster than Telegram. The built-in throttle is a seatbelt,
  not a licence: only message people who expect to hear from you.
- Registering numbers requires SMS/voice verification and often a captcha;
  there is no way around that, by design.
- The signal-cli data directory contains **full account keys**: anyone with
  those files controls the account. Keep the Docker volume private, never
  commit it, and don't expose the gateway (or port 7583) to the internet
  without the API key / a reverse proxy with TLS.
- This project is not affiliated with Signal; `signal-cli` is a community
  project. Respect Signal's Terms of Service.

## Development

```bash
pip install -e '.[dev]'
pytest          # fake-daemon tests, no Signal account needed
ruff check .
```

## Roadmap

- [ ] SSE endpoint as an alternative to WebSocket
- [ ] Persistent webhook subscriptions per account
- [ ] Attachment download endpoint (`getAttachment`)
- [ ] Typing indicators + read receipts helpers in the SDK
- [ ] Node/TypeScript SDK

Contributions welcome — especially typed endpoints for more signal-cli
methods (they're one `rpc_call` away, see `/v1/rpc`).

## Credits

- [signal-cli](https://github.com/AsamK/signal-cli) by AsamK — the client this
  gateway talks to (GPL-3.0). Install it separately; this repo does not vendor
  it.
- [libsignal](https://github.com/signalapp/libsignal) — Signal's protocol
  library, used by signal-cli, not by this code directly.
