# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this directory. This directory is git-tracked (repo: ancistrs/homelab). Backups via restic offsite + local tar archives.

## Companion documentation in PAOS vault

This homelab is also documented in Luk's Obsidian vault at `/Users/fink/PAOS/vault/Areas/homelab/`. Read both when doing non-trivial work:

- `/Users/fink/PAOS/vault/Areas/homelab/_homelab.md` - the human-facing architecture overview (services, ports, URLs, schedules, databases). Anchor file for the homelab area.
- `/Users/fink/PAOS/vault/Areas/homelab/Documentation/` - operational runbooks: `Firewall.md` (macOS pf rules), `Fresh Start.md` (rebuild from backup), `PostgreSQL Major Version Upgrade Guide.md`, `Basic Commands.md`.
- `/Users/fink/PAOS/vault/Resources/Cheat Sheets/` - reference notes for setups that span the homelab and other devices (e.g. `NextDNS Setup.md`).
- `/Users/fink/PAOS/vault/z_system/sessions/` - session notes from past cross-cutting work (security migrations, major rewrites).

When making structural changes to the homelab (new service, removed service, schedule changes, new folder, new database), update both this file AND `_homelab.md` in the same session - drift between the two erodes trust. The "Keeping PAOS documentation in sync" section below has the full list of triggers and conventions.

## Common Commands

```bash
# Start/restart the stack
docker compose up -d --remove-orphans

# Update all containers to latest images and cleanup
./scripts/update.sh

# View logs for a specific service
docker compose logs -f <service_name>

# Full backup (includes Postgres dumps, restic offsite)
./scripts/backup-homelab/backup-homelab.sh

# Prune unused Docker resources
./scripts/cleanup.sh

# Access Postgres shell
docker exec -it postgres psql -U admin -d postgres

# Rebuild all Python venvs after a pyenv upgrade
./scripts/rebuild-venvs.sh

# Manage LaunchAgents (use bootstrap/bootout, NOT legacy load/unload)
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<plist>
launchctl bootout gui/$(id -u)/<label>
launchctl kickstart gui/$(id -u)/<label>
launchctl print gui/$(id -u)/<label>
```

## Architecture

This is a Docker Compose homelab stack running on macOS. **All GUI services are reachable only via Tailscale** — each service has a `tailscale/tailscale` companion container ("sidecar") that joins the tailnet under the service name and proxies HTTPS via auto-issued Tailscale certs. The only public-facing surface is `n8n.ancistrs.net` (Cloudflare Tunnel) for `/webhook/*` and `/mcp/*` paths only — gated by Cloudflare Access service tokens + n8n API keys in the workflows. Plus `slack-webhook.ancistrs.net` for an n8n Slack-events flow (signature-verified by n8n itself, no Cloudflare Access). Docker runs via OrbStack. Some always-on services run as host-side LaunchAgents instead of Docker containers (currently `homelab.kb-query-server`, `user.whisper`) — they're integral to the system but live outside the compose stack because they need direct access to the host's Python venvs, MCP installations, or `claude` binary. These host services are surfaced inside Dozzle via per-service log-tail busybox containers (see [Log Tails](#log-tails) below).

### Networks

- **web**: External network — only `cloudflared` and `n8n` are on it (cloudflared routes the public webhook paths to n8n)
- **internal**: Bridge network for everything else, including all Tailscale sidecars

`docker-compose.yml` is split into three sections, each with services sorted alphabetically by name: `#-- INFRASTRUCTURE`, `#-- LOGS`, `#-- APPS`. New services should be inserted into the alphabetically correct slot in the appropriate section. **Tailscale sidecars (`<service>-ts`) live alphabetically right after their parent service** in the same section — see [Tailscale Sidecars](#tailscale-sidecars) below.

### Tailscale Sidecars

Each GUI service has a companion `<service>-ts` container running `tailscale/tailscale:latest`. The sidecar:

- Joins the tailnet via the reusable auth key in `${TAILSCALE_AUTH_KEY}` (set in `.env`)
- Registers as a tailnet device named after the service (`grafana`, `nocodb`, …) — accessible at `https://<service>.taildc3234.ts.net` from any tailnet member
- Auto-issues and renews its own Let's Encrypt cert via Tailscale's built-in HTTPS feature (enabled at the tailnet level)
- Reads `/config/serve.json` (bind-mounted from `tailscale/<service>/serve.json`) which configures `tailscale serve` to reverse-proxy to the actual service via Docker DNS (e.g. `http://grafana:3000`)
- Persists state in `/var/lib/tailscale` (bind-mounted from `tailscale/<service>/state/`) so the device identity survives container recreation

**Pattern variants:**

- **HTTPS proxy** (most services): `serve.json` defines port 443 with `HTTPS: true` and a `Web` proxy handler pointing at the parent service.
- **Raw TCP forward** (postgres): `serve.json` defines a TCP port with `TCPForward: "<host>:<port>"` — no HTTPS termination, traffic rides Tailscale's WireGuard encryption.
- **Host service proxy** (glances): `serve.json` proxies to `host.docker.internal:<port>` since glances runs on the Mac, not in Docker.

**Adding a new tailnet-only service** = paste an existing `<service>-ts` block, change the hostname/state-dir/serve-json paths, and create a matching `tailscale/<service>/{state/,serve.json}` directory pair.

### Infrastructure Services

- **cloudflared**: Cloudflare Tunnel for the only remaining public surface — `n8n.ancistrs.net/webhook*` and `/mcp*` paths
- **dozzle**: Container log viewer — `https://dozzle.taildc3234.ts.net` (tailnet only)
- **glances-ts**: Tailscale sidecar exposing the host-side glances dashboard at `https://glances.taildc3234.ts.net`
- **postgres**: Central database (pgvector/pgvector:pg18) used by n8n, paperless, grafana, nocodb, kb-sync. Bound to `127.0.0.1:5432` on the host so host-side scripts (kb-sync, kb-query-server) can connect via loopback. Off-host SQL access from the laptop is over Tailscale at `postgres.taildc3234.ts.net:5432` (VS Code PostgreSQL extension or any client). The loopback bind has no security cost — never exposed beyond `127.0.0.1`. **Known issue (2026-06-11, OrbStack 2.2.0):** the loopback forward for this specific binding is broken (TCP accepts, then drops before reaching postgres; fresh bindings on other ports work). Workaround: kb-sync and kb-query-server connect via `KB_PG_HOST=postgres.taildc3234.ts.net` set in their LaunchAgent plists. After an OrbStack update, test `psql -h 127.0.0.1` and remove the env var from both plists to return to loopback
- **redis**: Cache/queue for paperless (internal only, no tailnet exposure)
- **uptimekuma**: Status monitoring — `https://uptimekuma.taildc3234.ts.net`

### Log Tails

One `busybox tail -F` container per host LaunchAgent log file, named exactly like the underlying service. Bind-mounts the log file's **parent directory** to `/logs` (read-only) inside the container, then `tail -F /logs/<file>.log` follows the file by name. Directory-level mount is required because `logrotate`'s `create` pattern renames the file and creates a new one with a fresh inode — a file-level bind mount would stay pinned to the rotated-and-renamed file, and Dozzle would silently stop seeing new lines. With a directory mount, `tail -F` transparently picks up the new file after rotation.

| Container | Tails | `tail -n` |
|---|---|---|
| `backup-homelab` | `scripts/backup-homelab/backup-homelab.log` | 200 |
| `backup-personal` | `scripts/backup-personal/backup-personal.log` | 200 |
| `kb-query-server` | `scripts/kb-sync/kb-query-server.log` | 200 |
| `kb-sync` | `scripts/kb-sync/kb-sync.log` | 100 |
| `whisper` | `whisper/whisper.log` | 100 |

(`homelab.logrotate` intentionally has no tail container — it's silent by design on no-op runs; state lives in `scripts/logrotate/logrotate.state`. `paos.claude-remote` also has no tail container — its log is the Remote Control TUI captured through `script(1)`'s PTY (ANSI noise, not useful in Dozzle); check liveness via `launchctl print` instead.)

When adding a new LaunchAgent, add a matching tail container here. Pick the `tail -n` count based on chattiness (50 for "quiet on no-op" daily jobs, 100 for moderately chatty services, 200 for high-traffic always-on services or weekly batch runs).

### Application Services

- **grafana**: Metrics dashboards (depends on postgres) — `https://grafana.taildc3234.ts.net`
- **n8n**: Workflow automation (depends on postgres) — editor at `https://n8n.taildc3234.ts.net` (tailnet), public webhooks at `https://n8n.ancistrs.net/webhook/*` and `/mcp/*` (CF Tunnel + CF Access service token)
- **nocodb**: Airtable-like database UI (depends on postgres) — `https://nocodb.taildc3234.ts.net`
- **paperless**: Document management (depends on postgres + redis) — `https://paperless.taildc3234.ts.net`

### Database Schema

Each app has its own Postgres database **owned by its dedicated login role** (`x` owned by `x_user`), not by `admin`. The service user owns the database and therefore every object it creates (tables, sequences, app functions/triggers). New databases follow this pattern:

```sql
CREATE USER x_user WITH PASSWORD '…';
CREATE DATABASE x OWNER x_user;
```

That's the whole setup — no separate `GRANT … ON SCHEMA public` is needed. On PG15+ (incl. our pg18) the `public` schema is owned by the built-in `pg_database_owner` role, and the database owner is implicitly a member, so `x_user` can create tables in `public` purely by owning the database.

**Why the service user must own its objects, not `admin`:** app migrations that run `CREATE OR REPLACE FUNCTION` / `ALTER …` require ownership of the target object. If an object is left owned by `admin`, the app's migration role can't replace it and the container crash-loops on startup (`must be owner of function …`) — this is exactly what bit n8n's `increment_workflow_version` trigger after an image update. `admin` is the cluster **superuser**, so it keeps full access to every database regardless of ownership — handing ownership to the service user costs nothing and prevents this class of failure.

**Extension objects stay owned by `admin`** (pgvector, pg_trgm, uuid-ossp, fuzzystrmatch …). Extensions are installed by the superuser and app migrations never redefine their member functions, so admin-owned extension objects are expected and correct — only the app's *own* objects need to belong to the service user.

To retrofit an existing `admin`-owned database to this convention: `ALTER DATABASE x OWNER TO x_user;` then, connected to `x`, reassign any leftover **app** objects (skip extension members) with `ALTER TABLE/FUNCTION/SEQUENCE … OWNER TO x_user;`. Note that `ALTER TABLE … OWNER` needs an `ACCESS EXCLUSIVE` lock — stop or terminate any service holding a connection to that DB first (e.g. kb-query-server for `ancistrs`), or the statement blocks behind an open transaction.

- `n8n` / `n8n_user`
- `paperless` / `paperless_user`
- `nocodb` / `nocodb_user`
- `ancistrs` / `ancistrs_user` (kb-sync: `kb_index` table with pgvector embeddings)

### Secrets

All credentials stored in `.env` file. Key variables:

- `POSTGRES_ADMIN_PASSWORD`, `*_DB_PASS` for each service
- `CLOUDFLARE_TUNNEL_TOKEN`
- `N8N_ENCRYPTION_KEY`, `NOCODB_JWT_SECRET`
- `REDIS_PASSWORD`
- `TAILSCALE_AUTH_KEY` (reusable auth key, ~90-day expiry — used by all `<service>-ts` sidecars to join the tailnet)
- `GRAFANA_PASSWORD`
- `OPENROUTER_API_KEY` (used by kb-sync embedding/OCR)
- `OPENROUTER_EMBEDDING_MODEL`, `OPENROUTER_OCR_MODEL` (model names, change in .env to swap models)
- `COHERE_API_KEY` (used by kb-query for reranking)
- `COHERE_RERANK_MODEL` (rerank model name, change in .env to swap models)

### Scheduled Tasks (LaunchAgents)

Plists live in `~/Library/LaunchAgents/` (prefixes in use: `homelab.*`, `user.*`, `com.*`, `paos.*`). Each agent logs to a single `.log` file next to its script (stdout and stderr both redirected there).

| Label | Script | Log | Schedule | Purpose |
| --- | --- | --- | --- | --- |
| `homelab.backup-homelab` | `scripts/backup-homelab/backup-homelab.sh` | `scripts/backup-homelab/backup-homelab.log` | Weekly Sun 03:00 | Full backup + restic offsite |
| `homelab.personal-backup` | `scripts/backup-personal/backup-personal.sh` | `scripts/backup-personal/backup-personal.log` | Weekly Sun 04:00 | Personal backup (non-homelab) |
| `user.whisper` | `whisper/whisper.sh` | `whisper/whisper.log` | Always-on (KeepAlive) | Local Whisper transcription server |
| `homelab.kb-sync` | `scripts/kb-sync/kb-sync.sh` | `scripts/kb-sync/kb-sync.log` | Daily 02:17 | Knowledge base embedding sync |
| `homelab.kb-query-server` | `scripts/kb-sync/kb-query-server.sh` | `scripts/kb-sync/kb-query-server.log` | Always-on (KeepAlive) | KB semantic search API (port 8100) |
| `homelab.logrotate` | `scripts/logrotate/logrotate.sh` | `scripts/logrotate/logrotate.log` | Daily 01:15 | Rotate flat-file LaunchAgent logs (uses Homebrew `logrotate` with `copytruncate` for always-on services) |
| `com.glances.webui` | `/opt/homebrew/bin/glances -w` | `/tmp/glances.log` | Always-on (KeepAlive) | Glances host monitoring (port 61208, surfaced on tailnet via `glances-ts` sidecar) |
| `paos.claude-remote` | `scripts/claude-remote-paos.sh` | `scripts/claude-remote-paos.log` | Always-on (KeepAlive) | Claude Code Remote Control (single-session `--remote-control`) for `~/PAOS`; `/clear` for fresh context |

Scripts are designed to be quiet on no-op runs (no log output when nothing changed).

### Local Scripts (non-Docker)

Scripts and host-service code live under `scripts/` and `whisper/`, backed up as a single archive excluding `*.log` and `*.err` files:

- **scripts/backup-homelab/**: Weekly homelab backup script
- **scripts/backup-personal/**: Weekly personal restic backup (Documents, Pictures, PAOS, etc.)
- **scripts/cleanup.sh**: Prune unused Docker resources
- **scripts/update.sh**: Update all containers to latest images
- **scripts/rebuild-venvs.sh**: Rebuild all Python venvs after a pyenv upgrade, restart services, poll health checks
- **scripts/firewall.sh** + **firewall-restore.txt**: macOS pf firewall apply + restore rules
- **scripts/run_shortcut.sh**: Run an Apple Shortcut by name, optionally piping JSON stdin
- **scripts/claude-remote-paos.sh**: Launches the persistent `paos.claude-remote` remote-control session
- **scripts/kb-sync/**: Knowledge base embedding sync + query API (see `scripts/kb-sync/CLAUDE.md`)
- **scripts/logrotate/**: Daily log rotation for all flat-file LaunchAgent logs via Homebrew `logrotate`. Config in `homelab.conf`. State in `logrotate.state`. Always-on services use `copytruncate` (preserves the file inode so uvicorn keeps writing); batch scripts use `create`. See [Log rotation](#log-rotation) below.
- **tailscale/**: Per-service Tailscale sidecar state directories (`<service>/state/`) and serve configs (`<service>/serve.json`). Bind-mounted into each `<service>-ts` container. Backed up — losing a state dir would force the device to re-auth and get a new tailnet IP (cert auto-reissues, but identity is lost briefly).
- **whisper/**: Local Whisper transcription server (uvicorn, runs from a Python venv at `~/.venvs/whisper`). Top-level directory (not under `scripts/`) — backed up separately.

### Backup Strategy

`scripts/backup-homelab/backup-homelab.sh` runs weekly (Sunday 03:00). Two-tier approach: local tar archives for fast restores + restic offsite for deduplication and history.

**Local archives** (in `backups/`, rotated to keep only most recent):

1. Snapshots meta files (docker-compose.yml, .env, CLAUDE.md, .gitignore, .env.example)
2. Archives each service data directory with zstd compression (falls back to gzip): `uptimekuma`, `n8n`, `paperless`, `grafana`, `nocodb`, `whisper`, `tailscale`
3. Runs `document_renamer` on paperless before archiving
4. Archives `scripts/` directory (excluding `*.log`)
5. Dumps all Postgres databases via `pg_dump -Fc` (auto-discovers databases, no hardcoded list)
6. Dumps Postgres globals (roles, permissions) via `pg_dumpall --globals-only`

**When adding a new service with a Postgres database**, no backup-script changes are needed (auto-discovered). **When adding a new top-level service data dir** (or a new Tailscale sidecar state dir at `tailscale/<service>/`), add a corresponding `tar_dir` line and a `restic backup` argument in [`scripts/backup-homelab/backup-homelab.sh`](scripts/backup-homelab/backup-homelab.sh).

**Restic offsite** (backs up source directories directly for proper block-level deduplication):

- All service data dirs, scripts/, docker-compose.yml, .env, .gitignore, .env.example, and pg dumps
- Excludes loaded from `~/.restic/excludes/common.txt` and `~/.restic/excludes/homelab.txt`
- Retention: last 2 + 4 weekly + 6 monthly
- Config expected at `~/.restic/homelab.env`

**Note:** The `postgres/` data directory is intentionally not archived directly — live Postgres files cannot be safely copied. The `pg_dump` approach produces consistent, portable, version-independent dumps.

### Log rotation

Two surfaces, two mechanisms — both invisible from `docker-compose.yml`:

**1. Docker container logs** — set at the **OrbStack daemon level** in `~/.orbstack/config/docker.json`:

```json
{
  "log-driver": "local",
  "log-opts": { "max-size": "10m", "max-file": "5" }
}
```

Every container automatically inherits this driver and caps at ~50 MB of log history (10 MB × 5 files). No `logging:` blocks needed in `docker-compose.yml`. To edit and reload: `orbctl config docker` (opens the file in `$EDITOR` and auto-restarts the Docker engine on save). Existing containers must be recreated with `docker compose up -d --force-recreate` to pick up driver changes — the daemon default only applies at container creation.

**2. Flat-file LaunchAgent logs** — Homebrew `logrotate` (`/opt/homebrew/sbin/logrotate`), triggered daily at 01:15 by the `homelab.logrotate` LaunchAgent. Config lives in [`scripts/logrotate/homelab.conf`](scripts/logrotate/homelab.conf). Two patterns:

- **Always-on services** (uvicorn-served: kb-query-server, whisper) → `copytruncate` (10 MB threshold, 7 archives, gzip with delaycompress). The process holds the file open continuously, so the rotation copies content to an archive then truncates the original in place; the inode is preserved and uvicorn keeps writing to the same file.
- **Batch scripts** (backup-homelab, backup-personal, kb-sync) → `create 644 fink staff` (weekly, 8 archives, gzip with delaycompress). The script opens the log file fresh each run, so we can safely rename the old file and create a new empty one.

To add a new log file: append a stanza to `homelab.conf` following one of the two patterns above. To force-rotate now: `/opt/homebrew/sbin/logrotate --force --state /Users/fink/PAOS/code/homelab/scripts/logrotate/logrotate.state /Users/fink/PAOS/code/homelab/scripts/logrotate/homelab.conf`. Logs intentionally NOT covered: `n8n/n8nEventLog*.log` (n8n manages its own retention via `EXECUTIONS_DATA_PRUNE`), `uptimekuma/error.log` (managed by uptime-kuma).

## Working with This Directory

### Adding New Services

When adding or modifying services:

1. Use the Context7 MCP server (`mcp__context7__resolve-library-id` and `mcp__context7__query-docs`) to get up-to-date documentation for the service/image
2. For deeper research on security hardening, recommended env vars, or known pitfalls, invoke the `perplexity-research` skill
3. Follow the existing `docker-compose.yml` conventions:
   - Insert the service into the alphabetically correct slot in the appropriate `#--` section (`INFRASTRUCTURE`, `LOGS`, or `APPS`)
   - Use `security_opt: [no-new-privileges:true]` on all containers
   - Add health checks where applicable
   - Connect to appropriate networks (`web` for external access, `internal` for DB/service communication)
   - Set `restart: unless-stopped`
   - Use postgres dependency with `condition: service_healthy` if the service needs a database
   - Do **not** add a `logging:` block — every container inherits the daemon-level `local` driver from `~/.orbstack/config/docker.json` automatically
4. If the new service needs a Postgres database, follow the convention from [Database Schema](#database-schema): `CREATE USER x_user WITH PASSWORD '…'; CREATE DATABASE x OWNER x_user;` — the service user owns the database (and therefore everything it creates). The new database will be picked up automatically by the weekly backup's `pg_dump` auto-discovery.
5. **If the new service has a GUI** that should be reachable on the tailnet, add a Tailscale sidecar (see [Tailscale Sidecars](#tailscale-sidecars)):
   - Create `tailscale/<name>/state/` and `tailscale/<name>/serve.json` (copy from an existing service like `tailscale/grafana/`, change the `Proxy` target hostname:port)
   - Add a `<name>-ts` block in `docker-compose.yml`, alphabetically right after the parent service in the same section. Use the same env block as the existing sidecars (`TS_AUTHKEY`, `TS_HOSTNAME=<name>`, `TS_STATE_DIR`, `TS_USERSPACE=true`, `TS_SERVE_CONFIG`). Networks: `[internal]`. **Do not set `hostname:`** — that would cause Docker DNS to register the sidecar under the parent service's name, breaking proxy resolution
   - Add `tar_dir "$STACK_DIR/tailscale/<name>" "tailscale/<name>"` and a `restic backup` line in [`scripts/backup-homelab/backup-homelab.sh`](scripts/backup-homelab/backup-homelab.sh) — *unless* you backed up the whole `tailscale/` dir at once (current pattern), in which case nothing extra needed.

### Adding New LaunchAgents

When adding a new always-on or scheduled host-side service:

- Use `launchctl bootstrap`/`bootout` (not legacy `load`/`unload`)
- Point both `StandardOutPath` and `StandardErrorPath` to a single `.log` file next to the script (the homelab convention is one combined log per agent — no separate `.err` files)
- Keep scripts quiet on no-op runs to avoid log bloat
- New scripts go under `scripts/<name>/` (backed up as a single archive; `*.log` and `*.err` are excluded by the backup)
- Add a matching log-tail busybox container to `docker-compose.yml` under the `#-- LOGS` section so the new agent's log shows up in Dozzle (see [Log Tails](#log-tails) for the pattern)
- Add a matching stanza to [`scripts/logrotate/homelab.conf`](scripts/logrotate/homelab.conf) so the log doesn't grow forever — use `copytruncate` for always-on (uvicorn-style) services, or `create 644 fink staff` for batch scripts that exit between runs (see [Log rotation](#log-rotation) for the rules)
- Add a row to the [Scheduled Tasks (LaunchAgents)](#scheduled-tasks-launchagents) table above

### Keeping PAOS documentation in sync

This homelab is mirrored in Luk's personal Obsidian vault at [`/Users/fink/PAOS/vault/Areas/homelab/_homelab.md`](/Users/fink/PAOS/vault/Areas/homelab/_homelab.md). It is the human-facing reference Luk reads from his phone, his iPad, and his desktop search, and it is the anchor file for the entire `Areas/homelab/` folder in his vault. **After any structural change to the homelab, update this file in the same session so it doesn't drift.** Drift is hard to detect and erodes trust in the doc.

**Update the PAOS file when you:**

- Add, remove, or rename a Docker service → update the **Docker Services** table (insert into the alphabetical slot, set the right `Label`)
- Add, remove, or rename a host-side LaunchAgent service (kb-query-server, whisper, …) → update the **Host Services** table
- Add, remove, or change the schedule of any LaunchAgent → update the **Scheduled Tasks (LaunchAgents)** table
- Create, remove, or rename a folder under `scripts/` or any top-level homelab directory → update the **Folder Structure** block
- Create or drop a Postgres database → update the **Databases** table
- Make an architectural change that affects the overall picture (new service category, new security model, new external dependency, etc.) → update the **Overview** paragraph
- Add or change a service email address → update the **Emails** list

**Conventions used by `_homelab.md`** (match these when you edit):

- **Service names are code names**, not friendly display names. Use `postgres` not "PostgreSQL 18 (pgvector)". The container name in `docker-compose.yml`, the LaunchAgent label, and the database name are the source of truth.
- **All tables and lists are sorted alphabetically** by their primary key (container name, label, database name, folder name). No grouping, no historical order, no "important first". Just A→Z.
- **The Docker Services table is one big alphabetical list across all 19+ rows** — do not split into per-label sub-tables. The `Label` column (`infrastructure` / `logs` / `apps`) shows the categorization, and a clarifying note above the table explains that `logs` rows are busybox tail viewers for host services.
- **The Folder Structure block** keeps the meta files (`.env`, `CLAUDE.md`, `docker-compose.yml`) at the top, then top-level service data dirs alphabetically, then `scripts/` subdirs alphabetically. Comments after `#` describe what each directory is for.
- **Wikilinks (`[[X]]`) belong below the relevant table as a footnote line**, not inside table cells. Cells need to stay clean for alphabetical sorting and visual scanning.
- **Each service in Host Services has a one-line purpose description** so future-Luk knows why it exists without opening any other file.
