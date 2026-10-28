# ePIC output registration in PanDA

`swf_epicprod.panda.EpicAdderPlugin` is the ePIC adder, selected through
PanDA's `adder_plugins` configuration for both `epic` (new submissions)
and `wlcg` (legacy tasks). It subclasses the upstream `AdderSimplePlugin` and keeps
its Rucio registration, retries and error reporting.

The ePIC plugin excludes file records whose status is `nooutput` or
`failed` during registration, following `AdderAtlasPlugin`'s rule. It
restores the original file list in a `finally` block, so AdderGen retains
every file record for finalization. It does not change a job's result or
suppress registration errors for files that are present.

AdderGen marks an optional log omitted from the pilot's report as
`nooutput`. An ePIC log held in S3 is such a file
([LOG_STAGEOUT_FALLBACK.md](LOG_STAGEOUT_FALLBACK.md)). Registering its
placeholder at size zero creates a false catalog record, or a bytes
mismatch when a historical log already has the same name. Tasks 41039
and 41040 encountered the latter on 22 October 2026. Their reused task
names are a separate PCS allocation defect; this plugin does not rename
tasks, delete historical logs, or retry already failed jobs.

## Installation

Build a wheel from the committed swf-epicprod revision and install it
with `/opt/panda/bin/pip install --no-deps --force-reinstall` on
pandaserver01. PanDA core source remains the maintainers' installation.
Keep a copy of the previous ePIC package and live configuration.

The registration line in `/opt/panda/etc/panda/panda_server.cfg` is:

```ini
adder_plugins = wlcg:swf_epicprod.panda.EpicAdderPlugin:EpicAdderPlugin,epic:swf_epicprod.panda.EpicAdderPlugin:EpicAdderPlugin
```

The scripted configuration edit and the installed-class check live in
[distcomp-services/panda-server](https://github.com/BNLNPPS/distcomp-services/tree/main/panda-server).
Run `gate/adder_nooutput.py` as `atlpan` with the server's environment.
It exercises actual PanDA JobSpec and FileSpec objects with mocked Rucio
registration: absent-only, failed-only, present-only and mixed files.
Then load the configured plugins in a fresh process and require both
VOs to select `EpicAdderPlugin` before restarting `panda_daemon`, which
runs the adders. The HTTP listener and JEDI do not need a restart.

Verify fresh adder results from production after the restart, keeping
historical failures separate from new outcomes. If plugin loading or
the installed-class check fails, restore the saved configuration before
any restart. Record the installed revision, configuration backup,
restart time and observed result in distcomp-services `CHANGES.md`.
