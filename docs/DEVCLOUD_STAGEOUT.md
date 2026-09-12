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

# Lifecycle: logs and job reports expire on their own, after 7 days;
# the pilot prefix (section 4) is under neither rule.
aws s3api put-bucket-lifecycle-configuration \
    --bucket epic-devcloud-stageout \
    --lifecycle-configuration '{"Rules": [
        {"ID": "expire-logs", "Status": "Enabled",
         "Filter": {"Prefix": "logs/"}, "Expiration": {"Days": 7}},
        {"ID": "expire-reports", "Status": "Enabled",
         "Filter": {"Prefix": "reports/"}, "Expiration": {"Days": 7}}]}'

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

## 4. The pilot prefix: public pilot tarballs

The bucket also serves the pilot tarballs production operations builds
(OSG_SUBMISSION.md § Our canary pilot directory), under the `pilot/`
prefix, to consumers that fetch a pilot over HTTPS at every start: the
Perlmutter pilot launch ([NERSC_PERLMUTTER.md](NERSC_PERLMUTTER.md)),
and later any worker outside the SCDF perimeter. The pilot is public
code (the released pilot plus the commits on the production team's
public fork), so the prefix is readable by anyone; the rest of the
bucket, the logs and the write key stay private.

An object is named `pilot/pilot3-<version>-epicN.tar.gz`, the tarball's
own name (the prefix also holds what a pilot launch fetches beside the
pilot, such as the Event Service channel library
`es-channel-<python>-el9.tar.gz`), and is never overwritten and never
removed: a consumer pins
a name and a sha256, and an earlier pilot stays fetchable for as long
as anything might pin it. The URL is

```
https://epic-devcloud-stageout.s3.us-east-1.amazonaws.com/pilot/pilot3-<version>-epicN.tar.gz
```

**Bucket configuration (devcloud account holder; done 2026-09-12).**
Public reads on the prefix are granted by a bucket policy, which the
account's public-access block must permit; ACLs stay blocked. The
expiry rules name their prefixes, `logs/` and `reports/` (the job
reporter's, [JOB_REPORTING.md](JOB_REPORTING.md)), 7 days each, so
pilots do not expire.

```bash
aws s3api put-public-access-block --bucket epic-devcloud-stageout \
    --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=false,RestrictPublicBuckets=false

aws s3api put-bucket-policy --bucket epic-devcloud-stageout --policy '{
  "Version": "2012-10-17",
  "Statement": [{"Sid": "PublicPilotTarballs", "Effect": "Allow",
    "Principal": "*", "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::epic-devcloud-stageout/pilot/*"}]}'

aws s3api put-bucket-lifecycle-configuration --bucket epic-devcloud-stageout \
    --lifecycle-configuration '{"Rules": [
        {"ID": "expire-logs", "Status": "Enabled",
         "Filter": {"Prefix": "logs/"}, "Expiration": {"Days": 7}},
        {"ID": "expire-reports", "Status": "Enabled",
         "Filter": {"Prefix": "reports/"}, "Expiration": {"Days": 7}}]}'
```

The first object, `pilot/pilot3-3.14.3.3-epic3.tar.gz` (sha256
`b7b0e271…e02721`), was published 2026-09-12: a `git archive` of
commit 4e70e231 on the fork's `epic-es-server-api` branch, equal file
for file to the build npps0 had run from a local copy.

**Publishing a pilot (production operations).** The worker profile's
`PutObject` right covers the prefix; one copy from any host holding the
profile (npps0 today):

```bash
AWS_PROFILE=epic-stageout aws s3 cp pilot3-<version>-epicN.tar.gz \
    s3://epic-devcloud-stageout/pilot/pilot3-<version>-epicN.tar.gz
sha256sum pilot3-<version>-epicN.tar.gz
curl -sI https://epic-devcloud-stageout.s3.us-east-1.amazonaws.com/pilot/pilot3-<version>-epicN.tar.gz | head -1
```

The consumer then takes the new name and checksum: for Perlmutter, the
launcher's `PILOT_TARBALL_URL` and `PILOT_TARBALL_SHA256` lines, one
commit on `main`.

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
