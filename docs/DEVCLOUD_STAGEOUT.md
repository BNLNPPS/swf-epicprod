# Devcloud stage-out endpoint

An S3 bucket on the devcloud account, serving as the stage-out
destination for PanDA workers that run outside the SDCC network
perimeter. Perimeter-external workers (the NPPS GPU server today,
volunteer-class hosts later) cannot reach the SDCC dCache doors; the
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
blocked, as is every other SDCC-internal host).

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

## 3. Queue and storage configuration

Two queuedata fields for `BNL_NPPS_GPU` (CRIC):

- `acopytools`: activity `pl` (log stage-out) uses `s3` instead of
  `rucio`.
- `astorages`: activity `pl` points at `DEV_CLOUD_S3`.

`DEV_CLOUD_S3` is defined in the storage-data JSON the pilot loads at
startup. The pilot takes that catalog from a URL
(`--storagedata-url`), so the definition is a JSON file at any
reachable address — the git-sourced configuration pattern. The entry
supplies the s3 endpoint and bucket path from which the copytool
composes the object URL; the exact schema follows the ddmendpoints
format and is settled during integration.

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
