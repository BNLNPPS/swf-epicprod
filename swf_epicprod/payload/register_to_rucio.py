#!/usr/bin/env python3
"""Register files to Rucio: no_register upload + atomic register-and-attach.

Per file: skip if a predecessor already delivered it (AVAILABLE replica);
otherwise upload with no_register (pure data movement, RSE failover, dark-
leftover cleanup), then register the replica and attach it to its dataset in a
single add_files_to_datasets() call -- one transaction, so no orphaned replica.
The dataset, its rule and its metadata are created before the task is
submitted (RUCIO_REGISTRATION_CONTRACT.md); this script attaches to an
existing dataset and never creates one -- a missing dataset exits
NO_DATASET_EXIT, since a task whose datasets were not created was not
submitted through that path.

A genuine registration failure deletes the dark file and fails the file. A
catalog that cannot give a definite answer (RUCIO_RESILIENCE.md, Measure 2)
is different: nothing is deleted, the upload stands, and the run exits
PENDING_EXIT for an async registrar to complete later. A name already
holding different content diverts to a derived name rather than failing
(Measure 3) and is never deleted either -- validated data is not discarded
over a naming clash.

Same-name retries are safe: PCS regenerates the same DID, and the delivered/
leftover checks make a rerun idempotent.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from jsonschema import ValidationError
from jsonschema import validate as json_validate
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout
from rucio.client import Client
from rucio.client.uploadclient import UploadClient
from rucio.common.checksum import adler32, md5
from rucio.common.exception import (
    DatabaseException,
    DataIdentifierAlreadyExists,
    DataIdentifierNotFound,
    DuplicateContent,
    DuplicateRule,
    FileAlreadyExists,
    FileReplicaAlreadyExists,
    InvalidRSEExpression,
    NoFilesUploaded,
    NotAllFilesUploaded,
    ResourceTemporaryUnavailable,
    RSEWriteBlocked,
    RucioException,
    ServiceUnavailable,
    SourceNotFound,
)
from rucio.rse import rsemanager as rsemgr

# The catalog could not be reached to register the output, and could not be
# reached to say whether the output is there. run.sh reads this as a
# pending registration: the stage is recorded pending, the job keeps its
# finished physics and exits success, and the registrar completes the
# registration later (RUCIO_RESILIENCE.md, Measure 2). A code between this
# script and run.sh; never a job's exit code.
PENDING_EXIT = 81
# The output dataset does not exist: it is created at submission
# (RUCIO_REGISTRATION_CONTRACT.md § 2), so a job that finds none was not
# submitted through that path. A code between this script and run.sh, which
# records the registration failed; never a job's exit code.
NO_DATASET_EXIT = 84

RSE_FULL_THRESHOLD = 0.9
DATASET_META_PLUGIN = "ALL"

# DatabaseException wraps SQLAlchemy's DatabaseError (connection loss and
# deadlock included, but also non-transient causes); the client cannot tell
# these apart, so retrying it is a bounded, deliberate over-approximation.
# RSEWriteBlocked is excluded: it marks an RSE administratively closed for
# writing, not a blip, and upload_file() already fails fast on it to try
# the next candidate RSE instead of backing off in place.
TRANSIENT_EXC: tuple[type, ...] = (DatabaseException,)
IDEMPOTENT_OK: tuple[type, ...] = (
    DataIdentifierAlreadyExists,
    DuplicateContent,
    FileAlreadyExists,
    FileReplicaAlreadyExists,
    DuplicateRule,
)
# Connection timeout / server busy only
READ_TRANSIENT: tuple[type, ...] = (
    ServiceUnavailable,
    ResourceTemporaryUnavailable,
    RequestsConnectionError,
    RequestsTimeout,
)
# The catalog gave no definite answer, in either direction -- not "the
# catalog answered and the answer was no." RUCIO_RESILIENCE.md Measure 2:
# this is the one case a registration failure must not cost the job, since
# there is nothing wrong to report; the upload stands and an async
# registrar completes it later.
CATALOG_UNREACHABLE_EXC: tuple[type, ...] = READ_TRANSIENT + TRANSIENT_EXC


class ChecksumConflict(RuntimeError):
    """A DID with this scope:name already exists but with different content."""


class CatalogUnreachable(RuntimeError):
    """The catalog could not give a definite answer (connectivity or load),
    after retries were already exhausted. Distinct from a genuine failure:
    nothing is deleted, the upload stands for the async registrar to
    complete (RUCIO_RESILIENCE.md, Measure 2)."""


# Set once, in main(), before any other function runs; every function below
# reads these two directly rather than taking them as parameters -- both are
# fixed for the life of one run. scope varies only by call and stays an
# explicit parameter.
logger = logging.getLogger("rucio_register")
logger.addHandler(logging.StreamHandler())
logger.handlers[-1].setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.setLevel(logging.INFO)
client: Client = None  # type: ignore[assignment]


# The dataset tag schema (RUCIO_REGISTRATION_CONTRACT.md § 2): the dataset
# carries this at submission; this script never writes it, only compares its
# own reading of the job's output against it. Validation is best-effort for
# that comparison -- a run whose extraction is legitimately partial (a
# particle-gun run has no external input file, so no `generator` field) must
# not have its registration crash over it, so a validation failure skips the
# comparison rather than raising (see compare_dataset_metadata()).
SCHEMA_PATH = Path(__file__).parent / "rucio_ds_meta_schema.json"


def load_schema() -> dict[str, Any]:
    """Load the dataset metadata JSON schema from SCHEMA_PATH.

    :return: the parsed schema
    :raises FileNotFoundError: if SCHEMA_PATH does not exist
    """
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(f"Schema file not found at: {SCHEMA_PATH}")
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


METADATA_SCHEMA = load_schema()


def validate_metadata(metadata: dict[str, Any]) -> bool:
    """Validate a dataset metadata dict against METADATA_SCHEMA.

    :param metadata: the metadata to validate
    :return: True if valid
    :raises TypeError: if ``metadata`` is not a dict
    :raises ValueError: if it does not match the schema
    """
    if not isinstance(metadata, dict):
        raise TypeError("Metadata must be a JSON object (dictionary)")
    try:
        json_validate(instance=metadata, schema=METADATA_SCHEMA)
    except ValidationError as e:
        raise ValueError(f"Metadata validation failed: {e.message}") from e
    return True


# The output name forms the payload registers, so a derived name keeps the
# form: the attempt's mark goes before the level suffix, never after .root.
OUTPUT_SUFFIXES = (".eicrecon.edm4eic.root", ".edm4hep.root", ".hepmc3.tree.root")


def diverted_mark() -> str:
    """This attempt's own identity for a derived name: PANDAID when the
    pilot substituted one, else a UTC timestamp (manual/local runs)."""
    pandaid = str(os.environ.get("PANDAID") or "").strip()
    if pandaid:
        return pandaid
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def diverted_name(did_name: str, mark: str) -> str:
    """``<name>.p<mark>.<level suffix>``: the original name carrying this
    attempt's own mark, for an output whose name another attempt's
    different content already holds (RUCIO_REGISTRATION_CONTRACT.md,
    "Registration adopts or diverts, and never discards validated data").
    Since ``mark`` is this attempt's own identity (PANDAID), a within-task
    retry mints a fresh derived name rather than colliding with an earlier
    diverted attempt's.

    :param did_name: the original DID name that already holds other content
    :param mark: this attempt's own identity (see diverted_mark())
    :return: a derived DID name, same dataset, distinguishable suffix
    """
    for suffix in OUTPUT_SUFFIXES:
        if did_name.endswith(suffix):
            return f"{did_name[:-len(suffix)]}.p{mark}{suffix}"
    return f"{did_name}.p{mark}"


def with_retries(
    fn: Callable[[], Any],
    *,
    what: str,
    logger: logging.Logger,
    max_attempts: int = 5,
    base_delay: float = 3.0,
    max_delay: float = 60.0,
    transient: tuple[type, ...] = TRANSIENT_EXC,
    swallow: tuple[type, ...] = IDEMPOTENT_OK,
) -> Any:
    """Call ``fn`` with exponential backoff and full jitter.

    ``swallow`` exceptions are treated as success (return None); ``transient``
    ones are retried up to ``max_attempts``; anything else propagates.

    :param fn: zero-argument callable to invoke
    :param what: short label for this call, used in log messages
    :param logger: logger for retry/give-up messages
    :param max_attempts: maximum number of calls to ``fn``
    :param base_delay: initial backoff delay in seconds
    :param max_delay: cap on the backoff delay in seconds
    :param transient: exception types that are retried
    :param swallow: exception types treated as success
    :return: ``fn``'s return value, or None if a ``swallow`` exception fired
    :raises: whatever ``fn`` raises outside ``transient``/``swallow``, or a
        ``transient`` exception once ``max_attempts`` is exhausted
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except swallow as e:
            logger.info("%s: already present (%s) -> ok", what, type(e).__name__)
            return None
        except transient as e:
            if attempt >= max_attempts:
                logger.error("%s: giving up after %d attempts (%s)", what, attempt, e)
                raise
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay += random.uniform(0, delay)
            logger.warning(
                "%s: transient (%s) attempt %d/%d, retry in %.1fs",
                what,
                type(e).__name__,
                attempt,
                max_attempts,
                delay,
            )
            time.sleep(delay)


def read_with_retries(
    fn: Callable[[], Any],
    *,
    what: str,
    logger: Optional[logging.Logger] = None,
    max_attempts: int = 4,
    base_delay: float = 2.0,
) -> Any:
    """Retry a catalog read on transient failures with jittered backoff.

    Transient here means timeout or server-busy (READ_TRANSIENT); the jitter
    also de-synchronises concurrent jobs. Not-found/auth/invalid input are not
    transient and propagate immediately.

    :param fn: zero-argument read to invoke; materialise any generator inside
        it so the request executes within the retry
    :param what: short label for this call, used in log messages
    :param logger: logger for retry/give-up messages; defaults to the module
        logger if not given
    :param max_attempts: maximum number of calls to ``fn``
    :param base_delay: initial backoff delay in seconds
    :return: ``fn``'s return value
    :raises: whatever ``fn`` raises outside READ_TRANSIENT, or a READ_TRANSIENT
        exception once ``max_attempts`` is exhausted
    """
    return with_retries(
        fn,
        what=what,
        logger=logger or logging.getLogger("rucio_register"),
        max_attempts=max_attempts,
        base_delay=base_delay,
        transient=READ_TRANSIENT,
        swallow=(),
    )


def existing_did_matches(
    scope: str, name: str, bytes_: int, adler32_: str, events: Optional[int] = None
) -> bool:
    """Check whether a DID is already registered as the same work.

    Same work is judged by event count when both sides have one: a retry's
    bytes and checksum differ whenever the simulation is not reproducible,
    so checksum equality is neither necessary nor sufficient for "same
    work" (RUCIO_RESILIENCE.md, Measure 3 -- "a retry's bytes differ only
    because the simulation is not reproducible"). When ``events`` is given
    and the catalog's own ``events`` metadata is set, that comparison is
    decisive and checksum is not consulted at all. Only when events can't
    be compared (either side lacks a count -- --noregister callers, or a
    DID registered before events were written) does this fall back to
    bytes+checksum.

    :param scope: DID scope
    :param name: DID name
    :param bytes_: local file size to compare against the catalog
    :param adler32_: local adler32 checksum to compare against the catalog
    :param events: this run's own event count, when known, for the
        events-based same-work test
    :return: True if the DID exists and is the same work, False if absent
    :raises ChecksumConflict: if the DID exists and is different work
    """
    try:
        meta = read_with_retries(
            lambda: client.get_metadata(scope, name),
            what=f"get_metadata {scope}:{name}",
            logger=logger,
        )
    except DataIdentifierNotFound:
        return False
    recorded_events = meta.get("events")
    if events is not None and recorded_events is not None:
        if int(recorded_events) == int(events):
            return True
        raise ChecksumConflict(
            f"DID {scope}:{name} exists with events={recorded_events} but this "
            f"run made events={events}; refusing (conflict)."
        )
    r_adler = str(meta.get("adler32") or "").lstrip("0")
    l_adler = str(adler32_ or "").lstrip("0")
    r_bytes = meta.get("bytes")
    if (r_adler and l_adler and r_adler != l_adler) or (
        r_bytes is not None and int(r_bytes) != int(bytes_)
    ):
        raise ChecksumConflict(
            f"DID {scope}:{name} exists with bytes={r_bytes} adler32={meta.get('adler32')} "
            f"but local file is bytes={bytes_} adler32={adler32_}; refusing (conflict)."
        )
    return True


def ensure_dataset_exists(scope: str, name: str) -> None:
    """Confirm the dataset already exists; this never creates one.

    Datasets are created at task submission (RUCIO_REGISTRATION_CONTRACT.md
    § 2, the submission doer); a job finding none was not submitted
    through that path.

    :param scope: dataset scope
    :param name: dataset name
    :raises DataIdentifierNotFound: if the dataset does not exist
    :raises: a CATALOG_UNREACHABLE_EXC member if the catalog cannot answer
        at all, once retries are exhausted
    """
    read_with_retries(
        lambda: client.get_did(scope, name),
        what=f"get_did {scope}:{name}",
        logger=logger,
    )


def select_rses(rse_expression: str, max_used: float = RSE_FULL_THRESHOLD) -> list[str]:
    """Resolve an RSE expression to writable, deterministic candidate RSEs.

    RSEs at/above ``max_used`` used fraction go last (failover); the rest are
    randomised so concurrent jobs spread. Unknown usage counts as empty.

    :param rse_expression: RSE name or expression to resolve
    :param max_used: used fraction (0-1) at/above which an RSE is demoted
    :return: candidate RSE names, roomy ones (shuffled) before full ones
        (shuffled)
    :raises InvalidRSEExpression: if no RSE in the expression is both
        writable and deterministic
    """
    rses = read_with_retries(
        lambda: list(client.list_rses(rse_expression)),
        what=f"list_rses {rse_expression}",
        logger=logger,
    )
    usable = []
    for rse in (r["rse"] for r in rses):
        info = read_with_retries(
            lambda rse=rse: client.get_rse(rse), what=f"get_rse {rse}", logger=logger
        )
        if info.get("availability_write", True) and info.get("deterministic", True):
            usable.append(rse)
    if not usable:
        raise InvalidRSEExpression(f"{rse_expression!r}: no writable deterministic RSE")

    def used(rse):
        for u in read_with_retries(
            lambda rse=rse: list(client.get_rse_usage(rse)),
            what=f"get_rse_usage {rse}",
            logger=logger,
        ):
            if u.get("source") in ("storage", "rucio") and u.get("total"):
                return u["used"] / u["total"]
        return 0.0

    roomy = [r for r in usable if used(r) < max_used]
    full = [r for r in usable if r not in roomy]
    random.shuffle(roomy)
    random.shuffle(full)
    return roomy + full


def physical_delete(rse: str, scope: str, name: str) -> bool:
    """Delete a dark leftover from storage, if any. Called unconditionally
    before every upload attempt; a no-op (returns True) when there was
    nothing to remove -- Rucio's own protocol layer raises SourceNotFound for
    "nothing there" on delete (rsemanager.delete()'s documented contract,
    honored by every protocol backend), so no separate existence check is
    needed first.

    Uses a delete token if available, else x509.

    :param rse: RSE name
    :param scope: DID scope
    :param name: DID name
    :return: True if the file is gone or was already absent, False if a
        genuine deletion failure left it in place
    """
    try:
        try:
            token = client.get_delete_token(rse, scope, name, domain="wan")
        except Exception as error:
            logger.warning(
                "No delete token for %s:%s @ %s (%s); trying non-token auth",
                scope,
                name,
                rse,
                error,
            )
            token = None
        rsemgr.delete(
            rsemgr.get_rse_info(rse, vo=client.vo),
            [{"scope": scope, "name": name}],
            domain="wan",
            auth_token=token,
            logger=logger.log,
        )
        logger.warning("Dark leftover %s:%s on %s -> deleted", scope, name, rse)
        return True
    except SourceNotFound:
        return True
    except Exception as e:
        logger.error(
            "FAILED to delete dark file %s:%s from %s (%s) -- FLAG FOR "
            "A DARK-DATA SWEEPER",
            scope,
            name,
            rse,
            e,
        )
        return False


def upload_file(
    upload_client: UploadClient,
    *,
    path: str,
    scope: str,
    did_name: str,
    candidate_rses: list[str],
    max_attempts_per_rse: int,
    base_delay: float,
    transfer_timeout: Optional[int],
) -> str:
    """no_register upload with per-RSE retry and cross-RSE failover.

    Before each RSE, any file already there is a dark leftover of a prior
    attempt (no available replica -- caller checked) with possibly different
    bytes: the no_register upload would skip over it, so physical_delete()
    clears it unconditionally first (a no-op if there was nothing there); if
    it cannot be removed, skip the RSE.

    :param upload_client: Rucio UploadClient
    :param path: local file path
    :param scope: DID scope
    :param did_name: DID name
    :param candidate_rses: RSEs to try, in order
    :param max_attempts_per_rse: max upload attempts per RSE
    :param base_delay: base delay for exponential backoff
    :param transfer_timeout: optional transfer timeout in seconds
    :return: the RSE the file landed on
    :raises RuntimeError: if every candidate RSE failed
    :raises CatalogUnreachable: if the last failure was one the catalog
        gave no definite answer to
    """
    last_err = None
    for rse in candidate_rses:
        if not physical_delete(rse, scope, did_name):
            logger.error("Could not remove leftover on %s -> skip RSE", rse)
            continue
        # Hand-rolled rather than with_retries: RSEWriteBlocked must abandon
        # this RSE for the next candidate, while every other RucioException
        # (its own parent class) should back off and retry the same RSE --
        # a single isinstance transient tuple can't tell them apart.
        for attempt in range(1, max_attempts_per_rse + 1):
            item = {
                "path": path,
                "rse": rse,
                "did_scope": scope,
                "did_name": did_name,
                "no_register": True,
            }
            if transfer_timeout:
                item["transfer_timeout"] = transfer_timeout
            try:
                logger.info(
                    "Upload %s -> %s (try %d/%d)",
                    did_name,
                    rse,
                    attempt,
                    max_attempts_per_rse,
                )
                upload_client.upload([item], ignore_availability=True)
                logger.info("Uploaded %s -> %s", did_name, rse)
                return rse
            except (
                NoFilesUploaded,
                NotAllFilesUploaded,
                RSEWriteBlocked,
                RucioException,
            ) as e:
                last_err = e
                if isinstance(e, RSEWriteBlocked) or attempt >= max_attempts_per_rse:
                    break
                delay = base_delay * (2 ** (attempt - 1)) + random.uniform(
                    0, base_delay
                )
                logger.warning(
                    "Upload failed on %s (%s), retry in %.1fs",
                    rse,
                    type(e).__name__,
                    delay,
                )
                time.sleep(delay)
        logger.warning("RSE %s failed for %s -> failover", rse, did_name)
    # last_err is the real caught exception (RucioException's subclasses,
    # DatabaseException/ServiceUnavailable included, all land here via the
    # broad except above), so isinstance still tells transient from genuine
    # apart even though every attempt was absorbed into one RuntimeError.
    if isinstance(last_err, CATALOG_UNREACHABLE_EXC):
        raise CatalogUnreachable(
            f"catalog unreachable uploading {scope}:{did_name}: {last_err}"
        ) from last_err
    raise RuntimeError(f"All RSEs failed for {scope}:{did_name}: {last_err}") from last_err


def register_and_attach(
    *,
    scope: str,
    did_name: str,
    dataset: str,
    rse: str,
    bytes_: int,
    adler32_: str,
    md5_: Optional[str],
    events: Optional[int] = None,
) -> None:
    """Register the replica and attach it to its dataset in one transaction.

    A single add_files_to_datasets() with ``rse`` in the attachment: replica
    and attach commit or roll back together (no orphan). The dataset must
    already exist -- this never creates one (RUCIO_REGISTRATION_CONTRACT.md
    § 2).

    :param scope: DID scope
    :param did_name: DID name
    :param dataset: parent dataset name; must already exist
    :param rse: RSE the file was uploaded to
    :param bytes_: file size to register
    :param adler32_: adler32 checksum to register
    :param md5_: optional md5 checksum to register
    :raises ChecksumConflict: if already registered with different content
        (the file is left in place, not deleted)
    :raises CatalogUnreachable: if the catalog gave no definite answer (the
        file is left in place, not deleted)
    :raises RuntimeError: on any other registration failure (the dark file
        is deleted first)
    """
    already = (
        DataIdentifierAlreadyExists,
        DuplicateContent,
        FileAlreadyExists,
        FileReplicaAlreadyExists,
    )
    did: dict[str, Any] = {
        "scope": scope,
        "name": did_name,
        "bytes": bytes_,
        "adler32": adler32_,
    }
    if md5_:
        did["md5"] = md5_
    attachment = [{"scope": scope, "name": dataset, "rse": rse, "dids": [did]}]
    try:
        # swallow=() so "already" still surfaces to the verify step below
        # instead of being treated as success outright -- a false "ok" here
        # would leave a checksum conflict unnoticed.
        with_retries(
            lambda: client.add_files_to_datasets(attachment, ignore_duplicate=True),
            what=f"add_files_to_datasets {scope}:{did_name}",
            logger=logger,
            swallow=(),
        )
        logger.info(
            "Registered+attached %s:%s -> %s @ %s", scope, did_name, dataset, rse
        )
    except already:
        existing_did_matches(scope, did_name, bytes_, adler32_, events)
        logger.info(
            "%s:%s already registered+attached and matches -> ok", scope, did_name
        )
    except CATALOG_UNREACHABLE_EXC as e:
        # No definite answer, so nothing to clean up: the upload stands
        # exactly as it is for the async registrar to finish later. Deleting
        # it here would defeat the point of leaving it (Measure 2).
        logger.warning(
            "register+attach: catalog unreachable for %s:%s @ %s (%s) -> "
            "leaving the upload in place for the async registrar",
            scope,
            did_name,
            rse,
            e,
        )
        raise CatalogUnreachable(
            f"catalog unreachable for {scope}:{did_name} @ {rse}: {e}"
        ) from e
    except Exception as e:
        logger.error(
            "register+attach failed for %s:%s @ %s (%s) -> deleting dark file",
            scope,
            did_name,
            rse,
            e,
        )
        physical_delete(rse, scope, did_name)
        raise RuntimeError(
            f"register+attach failed for {scope}:{did_name} @ {rse}: {e}"
        ) from e


def apply_events(
    scope: str,
    dataset: str,
    file_dids: list[str],
    events: int,
) -> None:
    """Set the events count on each file DID, then verify the dataset's total.

    Rucio derives a dataset's ``events`` from the sum of its files' counts, so
    this both writes the per-file value and reads that derived total back to
    confirm it matches. A file whose count cannot be written, or a mismatch
    against the derived total, is logged, not raised.

    :param scope: DID scope
    :param dataset: dataset name whose derived total is verified
    :param file_dids: file DID names to set the count on
    :param events: event count to write on each file DID
    """
    for name in file_dids:
        try:
            client.set_metadata(scope, name, "events", int(events))
        except Exception as e:
            logger.error("events not set on %s:%s: %s", scope, name, e)
    try:
        counts = [f.get("events") for f in client.list_files(scope, dataset)]
        derived = client.get_metadata(scope, dataset).get("events")
    except Exception as e:
        logger.error("events on %s:%s unverified: %s", scope, dataset, e)
        return
    if any(c is None for c in counts):
        logger.error(
            "events on %s:%s unverified: %d/%d files carry no count",
            scope,
            dataset,
            sum(c is None for c in counts),
            len(counts),
        )
    elif derived != sum(counts):
        logger.error(
            "events on %s:%s unverified: derived %s != sum of files %d",
            scope,
            dataset,
            derived,
            sum(counts),
        )
    else:
        logger.info(
            "events on %s:%s verified: %d over %d files",
            scope,
            dataset,
            derived,
            len(counts),
        )


def compare_dataset_metadata(scope: str, dataset: str, local_meta: dict[str, Any]) -> dict[str, Any]:
    """Compare this job's own extracted metadata against the dataset's.

    The dataset's metadata is set at submission from the task's declared
    configuration (RUCIO_REGISTRATION_CONTRACT.md); this never writes it,
    only reports whether the software that actually ran agrees. A value the
    dataset lacks, or one that differs, is logged, never raised -- and a
    dataset with no metadata plugin data at all (a canary's flat dataset) is
    a reading, "declares nothing," not an error.

    :param scope: dataset scope
    :param dataset: dataset name to compare against
    :param local_meta: metadata this job extracted from its own output
    :return: {"agree": N, "differ": {key: {"dataset":..., "job":...}},
        "absent": [...]}, or {"declared": False, ...} when the dataset
        carries no metadata, or {"unread": "..."} when the catalog could
        not be read
    """
    try:
        declared = read_with_retries(
            lambda: client.get_metadata(scope, dataset, plugin=DATASET_META_PLUGIN),
            what=f"get_metadata {scope}:{dataset} ({DATASET_META_PLUGIN})",
            logger=logger,
        )
    except Exception as e:
        if "no metadata found" in str(e).lower():
            logger.info(
                "dataset %s:%s declares no metadata; the job read %d keys",
                scope,
                dataset,
                len(local_meta),
            )
            return {"declared": False, "agree": 0, "differ": {}, "absent": sorted(local_meta)}
        logger.error("metadata on dataset %s:%s unread: %s", scope, dataset, e)
        return {"unread": str(e)}
    differ = {
        k: {"dataset": declared.get(k), "job": v}
        for k, v in local_meta.items()
        if declared.get(k) is not None and declared.get(k) != v
    }
    absent = [k for k in local_meta if declared.get(k) is None]
    agree = len(local_meta) - len(differ) - len(absent)
    if differ or absent:
        logger.warning(
            "metadata on dataset %s:%s: %d agree, differ %s, absent %s",
            scope,
            dataset,
            agree,
            differ or "{}",
            absent or "[]",
        )
    else:
        logger.info(
            "metadata on dataset %s:%s agrees with the job's reading (%d keys)",
            scope,
            dataset,
            agree,
        )
    return {"agree": agree, "differ": differ, "absent": absent}


def main() -> int:
    """Parse CLI arguments, then upload and register the given files.

    :return: 0 if every file placed (and, unless --noregister, registered)
        successfully; PENDING_EXIT if nothing failed outright but the
        catalog gave no definite answer for at least one file (Measure 2 of
        RUCIO_RESILIENCE.md -- an async registrar completes it later);
        NO_DATASET_EXIT if a requested dataset does not exist (a task not
        submitted through the path that creates one); 1 if anything
        genuinely failed
    """
    parser = argparse.ArgumentParser(
        prog="Register to RUCIO",
        description="Registers files to RUCIO with optional dataset metadata comparison.",
    )
    parser.add_argument(
        "-f", dest="file_paths", nargs="+", required=True, help="Local file path(s)"
    )
    parser.add_argument(
        "-d",
        dest="did_names",
        nargs="+",
        required=True,
        help="DID name(s) for the RUCIO catalogue ('dataset/filename')",
    )
    parser.add_argument("-s", dest="scope", required=True, help="Scope")
    parser.add_argument(
        "-r", dest="rse", required=True, help="RSE name or expression (e.g. EIC-XRD)"
    )
    parser.add_argument(
        "--noregister",
        action="store_true",
        default=False,
        help="Upload only; skip catalog registration",
    )
    parser.add_argument(
        "--upload-metadata",
        dest="metadata_file",
        default=None,
        help="Path to a JSON file with this job's own extracted metadata "
        "(compared against the dataset's, never written)",
    )
    parser.add_argument(
        "--metadata-json",
        dest="metadata_json",
        default=None,
        help="JSON object of this job's own extracted metadata "
        "(parse_podio_metadata.py, eic-info); compared against the "
        "dataset's already-set metadata and written to "
        "$METADATA_COMPARE_OUT, never written to the dataset "
        "(RUCIO_REGISTRATION_CONTRACT.md)",
    )
    parser.add_argument(
        "--lifetime",
        type=int,
        default=None,
        help="Seconds each registered file DID -- and, since this is only "
        "ever a canary or trial run's own one-off dataset, the dataset "
        "DID too -- lives, so the run removes its own output; a "
        "precreated production dataset's rule and lifetime are set at "
        "submission, not here",
    )
    parser.add_argument(
        "--events",
        type=int,
        default=None,
        help="Event count written on each file DID and verified against "
        "the dataset's derived total",
    )
    parser.add_argument(
        "--select-max-used",
        type=float,
        default=RSE_FULL_THRESHOLD,
        help="Used fraction (0-1) at/above which an RSE is treated as "
        f"full and demoted to failover. Default: {RSE_FULL_THRESHOLD}",
    )
    parser.add_argument("--max-attempts-per-rse", type=int, default=3)
    parser.add_argument("--base-delay", type=float, default=5.0)
    parser.add_argument("--transfer-timeout", type=int, default=None)
    args = parser.parse_args()

    if len(args.file_paths) != len(args.did_names):
        raise ValueError("Number of file paths must match number of DID names.")
    for fp in args.file_paths:
        if not os.path.exists(fp):
            raise FileNotFoundError(f"File not found: {fp}")
    for did in args.did_names:
        if not os.path.dirname(did):
            raise ValueError(
                f"DID '{did}' has no parent dataset (expected 'dataset/filename')."
            )
    if args.metadata_file and args.metadata_json:
        raise ValueError("Cannot specify both --upload-metadata and --metadata-json")
    if not 0.0 < args.select_max_used <= 1.0:
        parser.error("--select-max-used must be in (0, 1]")

    # This job's own extracted metadata, for comparison only -- never sent
    # to the catalog as a write. A malformed --metadata-json is a real CLI
    # usage error and still raises; a metadata dict that fails the schema
    # (a particle-gun run has no external input file, so no `generator`
    # key -- a legitimately partial extraction, not a usage error) only
    # skips the comparison, since the comparison itself must never fail
    # the registration (RUCIO_REGISTRATION_CONTRACT.md § 2).
    local_meta = None
    if args.metadata_file:
        with open(args.metadata_file, "r", encoding="utf-8") as f:
            local_meta = json.load(f)
    elif args.metadata_json:
        try:
            local_meta = json.loads(args.metadata_json)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in --metadata-json: {e}") from e
    if local_meta is not None:
        try:
            validate_metadata(local_meta)
        except (TypeError, ValueError) as e:
            logger.warning(
                "extracted metadata does not match the schema (%s); "
                "comparison skipped, registration proceeds",
                e,
            )
            local_meta = None

    global client
    client = Client()
    upload_client = UploadClient(_client=client, logger=logger)
    do_register = not args.noregister
    try:
        candidates = select_rses(args.rse, max_used=args.select_max_used)
    except CATALOG_UNREACHABLE_EXC as e:
        # No RSE to even try against: every requested file is pending, not
        # failed -- the catalog gave no definite answer (Measure 2).
        logger.error("catalog unreachable resolving RSEs for '%s': %s", args.rse, e)
        logger.info("Summary: 0 ok, 0 failed, %d pending.", len(args.did_names))
        return PENDING_EXIT
    logger.info("RSE expression '%s' -> %s", args.rse, candidates)

    # Explicit dataset-existence gate, before any upload: one check per
    # distinct dataset among the requested files. A missing dataset means
    # this job's task was not submitted through the path that creates one
    # (RUCIO_REGISTRATION_CONTRACT.md § 2), so no bytes should move for
    # it -- a different, more specific outcome than a generic registration
    # failure, distinguished so run.sh never tries a stash for it.
    if do_register:
        for dataset in sorted({os.path.dirname(d) for d in args.did_names}):
            try:
                ensure_dataset_exists(args.scope, dataset, )
            except DataIdentifierNotFound:
                logger.error(
                    "output dataset %s:%s does not exist; it is created at "
                    "submission (RUCIO_REGISTRATION_CONTRACT.md § 2)",
                    args.scope,
                    dataset,
                )
                return NO_DATASET_EXIT
            except CATALOG_UNREACHABLE_EXC as e:
                logger.error(
                    "catalog unreachable checking dataset %s:%s: %s",
                    args.scope,
                    dataset,
                    e,
                )
                logger.info("Summary: 0 ok, 0 failed, %d pending.", len(args.did_names))
                return PENDING_EXIT

    # Phase 1: upload (skip files a predecessor already delivered).
    #
    # run.sh's landing check only rules out an available replica for RECO,
    # not for FULL (which can stay delivered while RECO is regenerated), so
    # upload_file()'s dark-leftover cleanup can't assume that here the way
    # its docstring does. Ask the catalog directly first; do_register-only,
    # since noregister log uploads use a fresh name every time.
    #
    # placed carries both the requested DID and the one actually used
    # (effective_did), since a genuine content clash diverts to a derived
    # name -- everything from here on registers under effective_did, while
    # failures/pending/diverted stay keyed by the original did, which is
    # what the caller (and Summary) tracks against.
    mark = diverted_mark()
    placed: list[
        tuple[str, str, str, str, int, str, str]
    ] = []  # did, effective_did, dataset, rse, bytes, adler, md5
    diverted: dict[str, str] = {}  # did -> effective_did, for ones that placed
    failures: list[str] = []
    pending: list[str] = []
    for path, did in zip(args.file_paths, args.did_names):
        size = os.stat(path).st_size
        adler = adler32(path)
        effective_did = did
        if do_register:
            try:
                if existing_did_matches(args.scope, did, size, adler, events=args.events):
                    logger.info(
                        "%s:%s already registered with matching bytes -> "
                        "delivered by an earlier attempt, skipping upload",
                        args.scope,
                        did,
                    )
                    continue
            except ChecksumConflict as e:
                derived = diverted_name(did, mark)
                logger.warning(
                    "%s:%s holds different content -> this attempt's output "
                    "registers as %s (%s)",
                    args.scope,
                    did,
                    derived,
                    e,
                )
                effective_did = derived
                diverted[did] = derived
            except CATALOG_UNREACHABLE_EXC as e:
                logger.warning(
                    "could not check existing DID %s:%s (%s); proceeding to upload",
                    args.scope,
                    did,
                    e,
                )
            except Exception as e:
                logger.warning(
                    "could not check existing DID %s:%s (%s); proceeding to upload",
                    args.scope,
                    did,
                    e,
                )
        try:
            rse = upload_file(
                upload_client,
                path=path,
                scope=args.scope,
                did_name=effective_did,
                candidate_rses=candidates,
                max_attempts_per_rse=args.max_attempts_per_rse,
                base_delay=args.base_delay,
                transfer_timeout=args.transfer_timeout,
            )
        except CatalogUnreachable as e:
            logger.error("catalog unreachable uploading %s: %s", effective_did, e)
            pending.append(did)
            continue
        except Exception as e:
            logger.error("FAILED upload %s: %s", effective_did, e)
            failures.append(did)
            continue
        placed.append(
            (did, effective_did, os.path.dirname(effective_did), rse, size, adler, md5(path))
        )

    if not do_register:
        logger.info(
            "--noregister: uploaded %d/%d, no catalog ops.",
            len(placed),
            len(args.file_paths),
        )
        return 1 if failures else 0

    # Phase 2: atomic register+attach
    datasets = {}
    for did, effective_did, dataset, rse, b, a, m in placed:
        datasets.setdefault(dataset, []).append((did, effective_did, rse, b, a, m))

    diverted_ok: list[str] = []  # derived DIDs actually registered, for DIVERTED_OUT
    metadata_comparison: dict[str, Any] = {}
    for dataset, items in datasets.items():
        attached: list[str] = []  # effective DIDs, for events/lifetime below
        for did, effective_did, rse, b, a, m in items:
            try:
                register_and_attach(
                    scope=args.scope,
                    did_name=effective_did,
                    dataset=dataset,
                    rse=rse,
                    bytes_=b,
                    adler32_=a,
                    md5_=m,
                    events=args.events
                )
                attached.append(effective_did)
                if did in diverted:
                    diverted_ok.append(effective_did)
            except CatalogUnreachable as e:
                logger.error(
                    "catalog unreachable registering %s: %s", effective_did, e
                )
                pending.append(did)
            except ChecksumConflict as e:
                logger.error("FAILED file %s: %s", effective_did, e)
                failures.append(did)
            except Exception as e:
                logger.error("FAILED register+attach %s: %s", effective_did, e)
                failures.append(did)
        if not attached:
            continue
        if local_meta:
            metadata_comparison[dataset] = compare_dataset_metadata(
                args.scope, dataset, local_meta
            )
        if args.lifetime is not None:
            # Every DID registered expires with the run's lifetime, the
            # files and their dataset alike, so the catalog forgets a
            # canary or trial run as its replica is reaped. This dataset is
            # never a precreated production one (register_and_attach only
            # ever runs against a dataset this job itself asked for), so
            # setting its lifetime here does not touch RUCIO_REGISTRATION_
            # CONTRACT.md § 2's precreated datasets, which carry their own.
            for name in attached + [dataset]:
                try:
                    client.set_metadata(args.scope, name, "lifetime", int(args.lifetime))
                except Exception as e:
                    logger.error("lifetime not set on %s:%s: %s", args.scope, name, e)
        if args.events is not None:
            apply_events(args.scope, dataset, attached, args.events)

    if diverted_ok:
        diverted_out = os.environ.get("DIVERTED_OUT")
        if diverted_out:
            try:
                with open(diverted_out, "w") as f:
                    f.write(",".join(diverted_ok))
            except OSError as e:
                logger.error(
                    "could not write DIVERTED_OUT (%s): %s", diverted_out, e
                )

    if metadata_comparison:
        compare_out = os.environ.get("METADATA_COMPARE_OUT")
        if compare_out:
            try:
                with open(compare_out, "w") as f:
                    json.dump(metadata_comparison, f)
            except OSError as e:
                logger.error(
                    "could not write METADATA_COMPARE_OUT (%s): %s", compare_out, e
                )

    failed = set(failures)
    pend = set(pending) - failed
    # Every DID ends up in exactly one bucket: already delivered (skipped,
    # in neither list), placed and attached, failed (upload, checksum
    # conflict, or registration), or pending (catalog gave no definite
    # answer) -- so total minus failed minus pending is ok, whether or not
    # a file was ever uploaded this run. A DID in both is reported failed:
    # a genuine failure is the more conservative outcome for run.sh's
    # stash-then-exit-78 fallback.
    logger.info(
        "Summary: %d ok, %d failed, %d pending.",
        len(args.did_names) - len(failed) - len(pend),
        len(failed),
        len(pend),
    )
    if failed:
        return 1
    if pend:
        return PENDING_EXIT
    return 0


if __name__ == "__main__":
    sys.exit(main())
