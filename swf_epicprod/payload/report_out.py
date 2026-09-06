#!/usr/bin/env python3
"""Send the payload's report out of the job, as an object.

A job runs where nothing can reach it, and PanDA keeps job metadata for
finished jobs only, so the account of a job that fails is otherwise lost
with the worker. This writes the report to object storage, which every
site permits outbound and which needs no service to receive it
(swf-epicprod docs/EPICPROD_PAYLOAD.md).

Standard library only, and deliberately so: the payload runs in the
campaign container, whose contents are not ours to choose, and the
current image carries no AWS library. The request is signed here with
AWS Signature Version 4, which is a documented hashing scheme over the
request, not a dependency.

Nothing here may cost a job. Every failure returns False with a reason
on stderr; nothing raises.

Usage:
  report_out.py --file payload-report.json --key <object key>
"""

import argparse
import datetime
import hashlib
import hmac
import os
import sys
import urllib.error
import urllib.request

ALGORITHM = "AWS4-HMAC-SHA256"
SERVICE = "s3"
TIMEOUT_S = float(os.environ.get("REPORT_OUT_TIMEOUT_S", "10"))


def _sign(key, message):
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret, date_stamp, region):
    """The date-, region- and service-scoped key AWS derives from the
    secret, so the secret itself never travels."""
    key = _sign(f"AWS4{secret}".encode("utf-8"), date_stamp)
    key = _sign(key, region)
    key = _sign(key, SERVICE)
    return _sign(key, "aws4_request")


def put_object(body, bucket, key, region, access_key, secret_key,
               endpoint=None, session_token=""):
    """Write one object. Returns (ok, detail).

    The request is signed as a single unchunked payload, whose hash is
    part of the signature, so a truncated or altered body is rejected by
    the service rather than stored.
    """
    host = (endpoint or f"{bucket}.s3.{region}.amazonaws.com").replace(
        "https://", "").replace("http://", "").rstrip("/")
    url = f"https://{host}/{key.lstrip('/')}"

    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()

    headers = {
        "host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if session_token:
        headers["x-amz-security-token"] = session_token
    signed_headers = ";".join(sorted(headers))
    canonical_headers = "".join(
        f"{name}:{headers[name]}\n" for name in sorted(headers))
    canonical_request = "\n".join([
        "PUT",
        "/" + key.lstrip("/"),
        "",
        canonical_headers,
        signed_headers,
        payload_hash,
    ])
    scope = f"{date_stamp}/{region}/{SERVICE}/aws4_request"
    to_sign = "\n".join([
        ALGORITHM, amz_date, scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signature = hmac.new(signing_key(secret_key, date_stamp, region),
                         to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers["Authorization"] = (
        f"{ALGORITHM} Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}")
    headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=body, headers=headers,
                                     method="PUT")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return True, f"{response.status} {url}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read()[:200].decode(errors='replace')}"
    except Exception as e:  # noqa: BLE001 - a report never fails a job
        return False, f"{type(e).__name__}: {e}"


def send(path, key):
    """Send one file as one object, taking the destination and the
    credential from the job environment. Returns True when written.

    Absent configuration is not a failure: a job whose environment names
    no bucket simply does not report this way.
    """
    bucket = os.environ.get("REPORT_OUT_BUCKET", "").strip()
    if not bucket:
        return False
    access_key = os.environ.get("REPORT_OUT_ACCESS_KEY_ID", "").strip()
    secret_key = os.environ.get("REPORT_OUT_SECRET_ACCESS_KEY", "").strip()
    if not access_key or not secret_key:
        print("report not sent: no credential in the environment",
              file=sys.stderr)
        return False
    try:
        with open(path, "rb") as f:
            body = f.read()
    except OSError as e:
        print(f"report not sent: {e}", file=sys.stderr)
        return False

    ok, detail = put_object(
        body, bucket, key,
        os.environ.get("REPORT_OUT_REGION", "us-east-1"),
        access_key, secret_key,
        endpoint=os.environ.get("REPORT_OUT_ENDPOINT", ""),
        session_token=os.environ.get("REPORT_OUT_SESSION_TOKEN", ""))
    print(f"report {'sent' if ok else 'not sent'}: {detail}",
          file=sys.stdout if ok else sys.stderr)
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--file", required=True)
    ap.add_argument("--key", required=True, help="the object key to write")
    args = ap.parse_args()
    send(args.file, args.key)
    # The exit code is always success: a job is never failed by its own
    # reporting.
    return 0


if __name__ == "__main__":
    sys.exit(main())
