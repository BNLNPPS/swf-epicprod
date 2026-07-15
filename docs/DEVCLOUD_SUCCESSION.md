# epic-devcloud.org Succession

`epic-devcloud.org` is the operator-provisioned cloud host of the ePIC
production system's external face. It carries `swf-remote` (open-internet
PanDA monitoring at `/prod/`) and corun-ai (the AI documentation and
assessment service at `/doc/`). This document is the devcloud counterpart
of `EPICPROD_SUCCESSION.md`: the operational inventory of what runs on
this host, under which accounts and credentials, what a repository clone
does not restore, and the re-establishment path. It records host state as
verified 2026-07-15; the live units and configuration files are the
canonical source for their entries. Application operating procedures are
in the application repositories: `corun-ai` `docs/deployment.md` and
`docs/job-system.md`, and the `swf-remote` README.

The host is an EC2 instance (m7i.xlarge, us-east-1, 1 TB gp3 volume,
elastic IP) in the operator's personal AWS account. The ePIC services
share it with unrelated personal applications behind a common ingress
layer; the shared PostgreSQL cluster holds the `swf_remote` and `corun`
databases alongside personal ones.

## Serving topology

Caddy owns the public ports and the ACME/TLS certificates for
`epic-devcloud.org`, proxying to an Apache backend on loopback
(`127.0.0.1:8443`). Apache serves the landing page at `/` from
`/var/www/epic-devcloud-landing`, `swf-remote` via mod_wsgi at `/prod`,
and corun-ai via mod_wsgi at `/doc`. Certbot and its certificates under
`/etc/letsencrypt` are retained only for rollback; `certbot.timer` is
disabled by design and Caddy's certificates are canonical.

## Services

| Unit | Runs | Role |
|---|---|---|
| `caddy.service` (system) | Caddy | Public ingress and TLS for all hostnames on the machine |
| Apache `apache2` (system) | mod_wsgi | `swf-remote` daemon (`/prod`), corun-ai daemon (`/doc`), landing page |
| `swf-remote-tunnel.service` (system) | autossh `-L 18443:localhost:443 pandaserver02` | Persistent SSH tunnel through which `swf-remote` proxies swf-monitor pages |
| `corun-worker` (supervisord) | `/var/www/corun-ai/worker.py` | AI job runner daemon (claude, Codex, Antigravity, DeepSeek runners) |
| PostgreSQL 15 (system) | shared cluster | `swf_remote` (Django internals only) and `corun` databases |

Deployment for both applications is rsync from the development tree by
each repository's `deploy/update_from_dev.sh`; `swf-remote` initial
provisioning is `deploy/setup-apache.sh`. corun-ai generation-code
changes additionally require `sudo supervisorctl restart corun-worker`
(`corun-ai` `docs/deployment.md`).

## Scheduled automation

User crontab (`admin`):

| Schedule | Job |
|---|---|
| every 10 min | `sync_users.sh` — corun-ai user sync from the `swf_remote` database |
| every 5 min | git pull of the `swf-*` repository clones |
| every 30 min | git pull of all repository clones |

The root crontab is empty. Nightly database and configuration backup
runs from the operator's personal automation (see Backups).

## Credentials

Variable names only. Application secrets live in each deployed tree's
`src/.env` (prod-local, gitignored, survives deploys).

| Credential | Variables | Held by |
|---|---|---|
| swf-monitor REST auth | `SWF_REMOTE_MONITOR_TOKEN`, `SWF_REMOTE_MONITOR_URL` | `swf-remote` app (requests traverse the tunnel) |
| swf-remote app | `SWF_REMOTE_SECRET_KEY`, `SWF_REMOTE_DB_*` | `swf-remote` app |
| corun-ai app | `CORUN_SECRET_KEY`, `CORUN_DB_PASSWORD`, `SYNC_SOURCE_DB_*` | corun-ai web and worker |
| corun-ai external services | `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, `SWF_MONITOR_MCP_TOKEN`, `CORUN_TJAI_MCP_URL/TOKEN`, `CORUN_GITHUB_TOKEN` | corun-worker job subprocesses |
| BNL SSH identity | `~/.ssh/id_rsa_tunnel`, ProxyJump gateway config, account `wenauseic` | `swf-remote-tunnel.service` |
| AI subscription auth | Claude Code, Codex, and Antigravity CLI login state under the admin home directory | corun-worker claude/Codex/Antigravity runners (claude jobs strip API keys to force subscription auth) |

Rotation procedure for the corun-ai credentials is documented in
`corun-ai` `docs/deployment.md` (edit `src/.env` in place, restart the
worker). All external-service credentials — Anthropic, OpenAI, Google,
DeepSeek, the GitHub token, and the backup destination — belong to the
operator's personal accounts; ownership and billing transfer with those
accounts, not with the host.

## Backups

A nightly job dumps all application databases on the cluster — including
`swf_remote` and `corun` — together with environment and configuration
files, and pushes them to the operator's personal Dropbox
(`tjai-backups/server/<date>`); current through 2026-07-15. There is no
volume-level (EBS snapshot) backup. Code is restored from the GitHub
repositories (`github.com/BNLNPPS/*`).

## Operator-bound operations

- **Domain and addressing.** `epic-devcloud.org` is registered through
  Route 53 Domains and its DNS zone hosted in Route 53, both in the
  operator's personal AWS account, which also holds the instance and the
  elastic IP. None of these are transferable to a successor.
- **Host administration.** Deployment scripts, service control, and
  `/etc` changes require sudo on the single `admin` account.
- **Tunnel identity.** The SSH tunnel authenticates as the operator's
  BNL account through the SDCC gateway; a successor needs their own BNL
  account and gateway access.
- **AI subscriptions.** The corun-worker runners authenticate against
  the operator's personal Claude, ChatGPT, and Google subscriptions via
  interactive login flows on this host.
- **Backup destination.** The nightly backup pushes to the operator's
  personal Dropbox.

## Known gaps

- The ingress configuration (Caddyfile, Apache vhosts), the
  drift-check/apply scripts, and the ingress runbook live in a private
  operator repository; the public repositories do not carry them.
  `swf-remote` `deploy/epic-devcloud.conf` predates the Caddy ingress
  (public `*:80`/`*:443` vhosts, certbot certificate paths) and no
  longer matches the live configuration.
- The landing page (`/var/www/epic-devcloud-landing`) is host state, in
  no repository.
- There is no volume-level backup; recovery depends on the nightly
  database/configuration dumps plus the repositories.
- The scripted recreation required by the re-establishment standard does
  not yet exist.

## Re-establishment

The standard for this host is scripted recreation: a new cloud instance,
a containerized stack, restore from backup — under a successor-controlled
domain name, followed by an update of the external-face references. The
monitor side is prepared for the name change: the external-face name is
specified in one code location (`EXTERNAL_FACE_DEFAULT` in
`swf-monitor/src/monitor_app/models.py`, runtime value in the SysConfig
key `external_face_base_url`; the corun-ai URL rides the
`CORUN_BASE_URL` environment variable). The recreation script is the
open work item. Because the domain, DNS, and elastic IP are personal
account resources, a successor re-establishes under a domain they
control rather than inheriting this one; the system treats the external
name as configuration.
