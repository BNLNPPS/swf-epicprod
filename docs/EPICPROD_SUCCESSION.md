# epicprod Succession

In its initial implementation epicprod has been the work of one
developer co-developing with AI. Consequently it is essential that the
succession path to extending development and infrastructure
responsibility to others be laid out. The pair programming workflow
goes a very long way to demonstrating every day that the repo doc,
code, and public doc are sufficient to bring a completely green AI to
expert developer level. But that is not the whole story, and this
document is designed to fill the gap.

The operational inventory of the ePIC production system: what runs,
where, on what schedule, under which credentials, and which operations
are bound to the operating account. It is the first document for anyone
who must operate, recover, or take over the system. Architecture and
component placement are in `ARCHITECTURE_MAP.md`; operating procedures
in `EPICPROD_OPS.md` and `EPICPROD_OPS_AGENT.md`; deployment in
`swf-monitor/docs/PRODUCTION_DEPLOYMENT.md`. This inventory records
host state on `pandaserver02` as verified 2026-07-15; the crontabs and
systemd units themselves are the canonical source for their entries.

## Repositories

| Repo | Role | Git policy |
|---|---|---|
| `swf-monitor` | Django web app, REST, MCP server, platform machinery (action stream, alarms, SSE, SysConfig); hosts the installed epicprod applications and runs the ops agent | branches + PRs, coordinated `infra/baseline-vNN` |
| `swf-common-lib` | `BaseAgent` and worker pool, ActiveMQ/STOMP messaging, REST logging | branches + PRs, coordinated |
| `swf-testbed` | CLI, orchestration, supervisord, workflow engine; the dev `.venv` the deploy copies | branches + PRs, coordinated |
| `swf-epicprod` | Production domain: `pcs`, ops agent doers, epicprod MCP tools, assessments, this doc set | direct-to-main while solo |
| `swf-remote` | External web face at `epic-devcloud.org`, separately deployed (not on this host) | direct-to-main, solo |

All origins are `github.com/BNLNPPS/*`. Never push directly to `main`
in the coordinated core repos.

## Services on pandaserver02

All swf units run as `User=wenauseic Group=eic` from the deploy tree
(`/opt/swf-monitor/current`), reading
`EnvironmentFile=/opt/swf-monitor/config/env/production.env`.

| Unit | Runs | Role |
|---|---|---|
| Apache `httpd` (system service) | mod_wsgi → `wsgi.py` | Serves `/swf-monitor/*`; holds no production credential |
| `swf-monitor-mcp-asgi.service` | uvicorn `mcp_asgi:application` on `127.0.0.1:8001` | MCP endpoint; also hosts the `jlab_rucio_*`/`bnl_rucio_*` toolsets |
| `swf-monitor-mcp-watchdog.service` + `.timer` | `scripts/mcp_watchdog.py --restart`, 1 min | Probes MCP health, restarts the ASGI worker on failure |
| `swf-panda-bot.service` | `manage.py panda_bot` | DISpatcher Mattermost bot (Anthropic-backed) |
| `swf-testbed-bot.service` | `manage.py testbed_bot` | Testbed Mattermost bot |
| `swf-epicprod-live.service` | `manage.py publish_epicprod_live` | Action stream → `#epicprod-live` publisher |
| `epicprod-ops-agent.service` | `agents/epicprod_ops_agent.py` | The credentialed executor — the only component holding PanDA/Rucio/xrootd credentials. Note the unit name does not match the `swf-*` prefix |

Backing stores (platform infrastructure, system-managed): PostgreSQL
`swfdb` (`/data/pgsql`), ActiveMQ/Artemis, Redis.

## Scheduled automation

User crontab (`wenauseic`):

| Schedule (ET) | Job |
|---|---|
| 01:30 | `backup-swfdb.sh` — pg_dump of `swfdb` to `/data/swf-shared/db-backups/`, verified and aged out |
| 02:15 | `enqueue-ops-message.py catalog_sync` — the nightly composite catalog-sync chain (steps and runbook in `EPICPROD_OPS.md`) |
| 03:00 | `update_epicdoc.sh` — doc-repo pulls and re-ingest for the epicdoc tools |
| 03:30 | `update_mcp_servers.sh` — pull and rebuild external MCP servers; bot restart if changed |
| 03:45 | `assessment-trigger-cron.sh --kind daily` — daily campaign assessment |
| 04:00 | `nightly-pull.sh` — safe git refresh of every repo under `/data/wenauseic/github` |
| 06:00 Mon | `assessment-trigger-cron.sh --kind weekly` — weekly campaign assessment |
| every 5 min | `swf-alarms-run` — production alarms engine |

Root crontab:

| Schedule | Job |
|---|---|
| every 2 min | `prodops-cleaner-killer.sh` — duplicate-agent reaping and MQ liveness ping, with restart of a dead ops agent |
| 03:30 | `prodops-cleaner-killer.sh --no-liveness --prune-days 30` — payload-log cache pruning |

The root crontab and the hand-installed bot/agent unit files under
`/etc/systemd/system/` are host state, not in git; a git pull restores
neither.

## Credentials

Secrets live in `~/.env` (mode 0600, `wenauseic`) and
`/opt/swf-monitor/config/env/production.env` (root-owned). Variable
names only; the boundary invariant is that the web tier and MCP server
hold no PanDA/Rucio/xrootd credential — only the ops agent does.

| Credential | Variables | Held by |
|---|---|---|
| PanDA OIDC token (`EIC.production`) | `PANDA_CONFIG_ROOT` (cached token), `PANDA_AUTH`, `PANDA_AUTH_VO` | ops agent |
| BNL Rucio x509 proxy | `X509_USER_PROXY`, `RUCIO_BNL_X509_PROXY` → `longproxy-for-rucio` (copies at `/data/wenauseic/` and `/etc/swf-monitor/`) | ops agent; ASGI `bnl_rucio_*` |
| EVGEN output proxy | `EVGEN_X509_PROXY` | ops agent EVGEN doers |
| JLab Rucio userpass (`eicread`) | `RUCIO_JLAB_USERNAME/PASSWORD/URL`, `RUCIO_AUTH_HOST` | ASGI `jlab_rucio_*`; Rucio snapshot doer |
| Anthropic | `ANTHROPIC_API_KEY` | PanDA bot (DISpatcher) |
| Mattermost | `MATTERMOST_TOKEN`, `EPICPROD_LIVE_TOKEN` | bots; live publisher |
| corun-ai | `CORUN_API_TOKEN`, `CORUN_BASE_URL`, `CORUN_CALLBACK_URL` | bot; assessment path |
| MCP bearer | `MCP_BEARER_TOKEN` | ASGI MCP; watchdog |
| DB / bus / app | `DB_USER/PASSWORD`, `ACTIVEMQ_USER/PASSWORD`, `SECRET_KEY` | web, ASGI, bots, agent |
| TLS trust | `REQUESTS_CA_BUNDLE`, `SSL_CERT_FILE` | all HTTPS/Rucio callers |

The nightly chain's `credential_expiry_check`
(`python -m swf_epicprod.credential_check`, warn window
`CREDENTIAL_EXPIRY_WARN_DAYS`, default 7 days) checks the PanDA token
and both proxies.

## Operator-bound operations

Operations that only the operating account (`wenauseic` / PanDA
identity `wenaus`) can perform:

- **PanDA production authorization and token renewal.** Submission
  requires `EIC/production` IAM membership carried in the operator's
  OIDC token; renewal is an interactive device flow that must run in a
  real shell (`EPICPROD_OPS.md`). There is no robot or service
  account; the agent reuses the operator's cached personal token.
  Transfer is administrative rather than technical: a successor with
  `EIC/production` membership authenticates on the host and the agent
  runs under their token.
- **x509 proxy renewal** for the Rucio and EVGEN proxies (manual;
  expiry silently stops the credentialed sweeps — the expiry check
  exists to warn first).
- **Deployment and service control** — the deploy scripts and
  `systemctl` restarts require sudo on this host, as do
  `production.env` edits and the root crontab.
- **External accounts** — the Anthropic key, Mattermost bot tokens,
  and corun-ai token belong to the operator's accounts.

## Known gaps

- No robot/service account for PanDA submission (roadmap in
  `EPICPROD_OPS_AGENT.md`); the submission identity is whichever
  operator last authenticated on the host.
- Ownership, billing, and rotation procedure for the external-service
  credentials (Anthropic, Mattermost, corun-ai) are undocumented.
- The root crontab and hand-installed unit files exist only as host
  state; no version-controlled copy.
- corun-ai and swf-remote operations run on `epic-devcloud.org` and
  are not documented from this host; `EXTERNAL_ACCESS.md` covers the
  contract, not the host operations.
- Apache/SSL lifecycle and volume-level backup belong to the SDCC
  system layer and have no local runbook.
- The external-face name (`epic-devcloud.org`) is intended to be a
  single configuration value but is currently embedded at several code
  sites (base template link, status probes, publisher default,
  corun-ai URL defaults); consolidation is pending.

## Re-establishment

A new AI session reaches working competence from the repositories and
documentation alone each working day, which demonstrates that the
documented system transfers. What a repository clone does not restore
is the running infrastructure: the accounts, tokens, sudo grants,
crontabs, and host provisioning enumerated above are bound to the
operating account.

The two hosts have different recovery paths. `pandaserver02` is BNL
infrastructure with system administration and established
responsibility lines; recovering or replacing the host is a
laboratory process, and succession there consists of the credential
and knowledge transfer this document records. `epic-devcloud.org` is
operator-provisioned. It carries the external monitoring face
(`swf-remote`) and the AI document service (corun-ai); an outage
degrades external access and assessment generation, and no
institutional support arrangement covers its recreation. The
re-establishment standard therefore applies to `epic-devcloud.org`: a
scripted recreation (new cloud instance, containerized stack, restore
from backup) under a successor-controlled domain name, followed by an
update of the external-face references in system configuration. The
domain name and its cloud addressing are held in personal accounts
and are not transferable; the system treats the external name as
configuration.
