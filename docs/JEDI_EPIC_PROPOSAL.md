# ePIC Direct Task Submission to JEDI

**From:** Torre Wenaus
**Date:** March 2026

## What We're Doing

The ePIC production monitor ([epic-devcloud.org/prod/](https://epic-devcloud.org/prod/)) composes physics configurations into fully specified production tasks. We want to submit these directly to JEDI via `Client.insertTaskParams()` — a complete `taskParamMap`, no script generation.

We'll use `GenTaskRefiner`. All ePIC jobs are containerized MC generation (no input datasets). Here's a representative submission:

```python
{
    "taskName": "group.EIC.26.02.0.epic_craterlake.p3001.e1.s1.r1",
    "userName": "wenaus",
    "vo": "eic",
    "workingGroup": "EIC",
    "prodSourceLabel": "managed",
    "taskType": "production",
    "processingType": "epicproduction",
    "taskPriority": 900,
    "container_name": "docker://eicweb/jug_xl:26.02.0-stable",
    "architecture": "",
    "transUses": "",
    "transHome": "",
    "noInput": true,
    "nFiles": 10,
    "nFilesPerJob": 1,
    "nEventsPerJob": 100,
    "coreCount": 1,
    "site": "BNL_EPIC_PROD_1",
    "log": {
        "dataset": "group.EIC:group.EIC.26.02.0.epic_craterlake.p3001.e1.s1.r1.log",
        "type": "template",
        "param_type": "log",
        "token": "local",
        "destination": "local",
        "value": "group.EIC.26.02.0.epic_craterlake.p3001.e1.s1.r1.log.${SN}.log.tgz"
    },
    "jobParameters": [
        {
            "type": "constant",
            "value": "EBEAM=10 PBEAM=100 DETECTOR_VERSION=26.02.0 DETECTOR_CONFIG=epic_craterlake ./run.sh"
        },
        {
            "type": "template",
            "param_type": "output",
            "token": "local",
            "destination": "local",
            "dataset": "group.EIC:group.EIC.26.02.0.epic_craterlake.p3001.e1.s1.r1",
            "value": "group.EIC.26.02.0.epic_craterlake.p3001.e1.s1.r1.${SN}.root",
            "offset": 1000
        }
    ]
}
```

We're ready to test with `prodSourceLabel: "test"` whenever you say go.
