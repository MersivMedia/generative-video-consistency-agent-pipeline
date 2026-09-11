# Deploying a public screening

The app is a single process that holds one screening in memory and journals it
to disk. It is not a cluster. What it needs to face an audience is TLS, a cache
in front of the segments, and a supervisor that restarts it — all of which are
external to the application.

Local development stays exactly as before:

```bash
python engine/server.py stories/the_signal.json --demo --port 8137
```

---

## 1. TLS and a real hostname

Do not expose the app's port directly. Plain HTTP is increasingly blocked on
mobile, odd ports are filtered on some networks, and the app has no business
terminating TLS.

`Caddyfile`:

```caddyfile
screening.example.com {
    # WebSocket upgrade must be proxied, not just HTTP. Without this the
    # viewer loads, shows the picture and never receives a vote window,
    # because /live silently fails to upgrade.
    reverse_proxy localhost:8137 {
        header_up X-Forwarded-For {remote_host}
        header_up X-Forwarded-Proto {scheme}
    }
}
```

Caddy handles the upgrade automatically and obtains a certificate on first
request. `X-Forwarded-For` matters: the token rate limiter reads it, and
without it every viewer appears to come from `127.0.0.1` and shares one cap.

Mounting under a path prefix works too, because every URL the client uses is
derived from `location.pathname` and every playlist emits relative segment
URLs:

```caddyfile
example.com {
    handle_path /thisway/* {
        reverse_proxy localhost:8137
    }
}
```

With TLS in front, the viewer's WebSocket automatically uses `wss://` — it
picks the scheme from `location.protocol`.

---

## 2. A CDN in front of the segments

This is the difference between a handful of viewers and an audience. Each
viewer pulls ~1-2 Mbps continuously; a hundred of them is 200 Mbps out of one
box that is also encoding video.

The routes are already split for it:

| Route | Cache | Why |
|---|---|---|
| `/segments/{rung}/seg_NNNN.ts` | `max-age=31536000, immutable` | a written segment never changes |
| `/stream/*.m3u8` | `no-store, must-revalidate` | rewritten on every publish |
| `/live`, `/token`, `/state` | never | live state |

So any CDN works with a default configuration that honours origin headers. The
one rule that must not be violated: **never cache the playlists.** A cached
playlist freezes a viewer at whatever the stream looked like when they first
connected, which presents as "it played for a while and stopped".

CORS is already open (`Access-Control-Allow-Origin: *`) so segments can be
served from a different hostname than the page.

Rough capacity: with segments on a CDN, origin egress is one fetch per segment
per edge rather than per viewer, and the box's limit becomes encoding rather
than bandwidth.

---

## 3. Supervision

The journal makes restarts safe — segments and decisions replay, already-decided
beats are skipped, and the playhead resumes where it left off. That only helps
if something actually restarts the process.

`/etc/systemd/system/thisway.service`:

```ini
[Unit]
Description=THIS WAY screening
After=network.target

[Service]
Type=simple
User=screening
WorkingDirectory=/srv/thisway
EnvironmentFile=/etc/thisway/env
ExecStart=/srv/thisway/.venv/bin/python engine/server.py \
          stories/the_signal.json --port 8137
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
```

`/etc/thisway/env` holds the API keys and is `0600`, owned by the service user.
It is never part of the repository.

Verify a restart resumes rather than starting over:

```bash
systemctl restart thisway
journalctl -u thisway -n 5
#   resumed: 20 segments, 3 decisions
#   resuming with 6 beats remaining
```

---

## 4. Disk

Segments accumulate at roughly **3.3 MB per 5 seconds** across all three ladder
rungs, so about 40 MB per minute, 2.4 GB per hour of screening. A long run
needs either a disk that accounts for that or a reaper for segments well behind
the playhead. Nothing prunes automatically, because deleting media a late
joiner might still request is a product decision.

The signing secret lives at `~/.cache/thisway/screening_secret` (or
`$THISWAY_SECRET`), `0600`. Losing it invalidates existing viewer tokens and
everyone is silently re-minted — acceptable for an anonymous screening, and far
better than a hardcoded default that would make every deployment forgeable.

---

## What this still is not

- **One process, one screening.** No horizontal scale. Two screenings means two
  processes on two ports.
- **Sybil resistance is bounded.** One signed token is one ballot across
  refreshes, reconnections and extra tabs, and the per-IP cap makes minting
  cost infrastructure rather than a keyboard shortcut. Someone with many
  addresses can still stuff a ballot; stopping that needs real accounts, which
  is a product decision.
- **No moderation, no auth, no admin surface.** Anyone with the URL watches and
  votes.
