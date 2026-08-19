# Devcloud stage-out endpoint

An S3 bucket on the devcloud account, serving as the stage-out
destination for PanDA workers that run outside the SCDF network
perimeter. Perimeter-external workers (the NPPS GPU server today,
volunteer-class hosts later) cannot reach the SCDF dCache doors; the
bucket gives them a reachable destination for job logs and other
small outputs. It is not a bulk-data store: an account-level quota
and a lifecycle expiry policy keep it small, and large outputs remain
the province of the lab storage systems.

No Rucio component is involved. The pilot's `s3` copytool
(`pilot/copytool/s3.py`) uploads with boto3 using an AWS credentials
profile held on the worker, selected by the `PANDA_PILOT_AWS_PROFILE`
environment variable. This is the volunteer-computing data path in
its final form: a worker anywhere on the internet, a set of scoped
object-store keys, one HTTPS PUT.

First consumer: the `BNL_NPPS_GPU` queue, whose jobs complete their
payloads but fail log stage-out because every RSE in the BNL EIC
Rucio catalog fronts `dcintdoor.sdcc.bnl.gov`, unreachable from the
worker's subnet (verified 2026-08-14: root:1094 and davs:443 both
blocked, as is every other SCDF-internal host).

Three parts: the bucket (devcloud account holder), the worker
credentials (worker host operator), and the queue configuration
(queue admin plus a storage-data JSON served from a URL).

## 1. Bucket (devcloud account)

```bash
aws s3 mb s3://epic-devcloud-stageout --region <region>

# Lifecycle: logs expire on their own.
aws s3api put-bucket-lifecycle-configuration \
    --bucket epic-devcloud-stageout \
    --lifecycle-configuration '{"Rules": [{"ID": "expire-logs",
        "Status": "Enabled", "Filter": {},
        "Expiration": {"Days": 30}}]}'

# One IAM user scoped to this bucket only, allowing PutObject,
# GetObject, ListBucket. Its access key pair is the worker credential.
```

Block public access (default). A self-hosted equivalent (MinIO on the
devcloud host) serves the same protocol if the deployment moves off
AWS; nothing else in this document changes.

## 2. Worker credentials

On each worker host, a standard AWS credentials profile:

```ini
# ~/.aws/credentials
[epic-stageout]
aws_access_key_id = <key>
aws_secret_access_key = <secret>
```

and in the pilot runner environment:

```bash
export PANDA_PILOT_AWS_PROFILE=epic-stageout
```

For volunteer deployment the profile ships in the client bundle with
keys scoped to write-only access.

## 3. Queue and storage configuration (implemented, no CRIC changes)

Both pieces are git-sourced from `tools/npps0/config/` and applied by
the pilot pass script; CRIC is not involved beyond the queue's
existence.

- `config/queuedata.json`: the queue's pilot-side behavior, with
  `acopytools.pl = ["s3"]`, `astorages.pl = ["DEV_CLOUD_S3"]`, and
  `s3` registered in `copytools`. The pass script places it in the
  run directory, where the BNL pilot wrapper prefers a local
  `queuedata.json` (`file://$PWD/queuedata.json`) over the
  CRIC-derived cache URL.
- `config/agis_ddmendpoints.json`: the storage catalog — a snapshot
  of the CRIC ddmendpoints set (so all lab endpoints still resolve)
  plus the `DEV_CLOUD_S3` entry: type `OS_LOGS`, non-deterministic,
  protocols pointing at
  `https://s3.us-east-1.amazonaws.com:443//epic-devcloud-stageout/logs`
  (the pilot's s3 copytool uses boto3, which rejects the `s3://`
  scheme carried by older catalog entries). The pass script applies
  the catalog by rewriting the `--storagedata-url` in a per-pass copy
  of the pilot wrapper to a `file://` reference to this file; the
  wrapper appends its own storage-data URL after all passthrough
  arguments, so seeding files alone does not take effect. The file is
  also seeded into the run directory under the pilot info system's
  cache filenames (`agis_ddmendpoints.json`,
  `agis_ddmendpoints.agis.*.json`).

The s3 copytool composes the object key as
`logs/<queue>/<log dataset>/<lfn>`, so log tarballs arrive under
`logs/BNL_NPPS_GPU/` in the bucket.

## Verification

From a worker host, in order:

```bash
# 1. Reachability (bucket endpoint, HTTPS)
curl -sI https://epic-devcloud-stageout.s3.<region>.amazonaws.com | head -1

# 2. Direct write with the worker profile
AWS_PROFILE=epic-stageout aws s3 cp /etc/hostname \
    s3://epic-devcloud-stageout/probe-$(date +%s)

# 3. End to end: submit a test task to the queue and confirm the job
#    reaches 'finished' with its log object present in the bucket.
```
