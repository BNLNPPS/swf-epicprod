#!/usr/bin/env python3

import argparse
import os
import sys
import json
import logging
from typing import Dict, Any
from rucio.client.uploadclient import UploadClient
from rucio.client import Client
from rucio.common.exception import InputValidationError, RSEWriteBlocked, NoFilesUploaded, NotAllFilesUploaded
from jsonschema import validate as json_validate, ValidationError

# The catalog could not be reached to register the output, and could not be
# reached to say whether the output is there. The run script reads this as a
# pending registration: the stage is recorded pending, the job keeps its
# finished physics and exits success, and the registrar completes the
# registration later (docs/RUCIO_RESILIENCE.md, Measure 2). It is a code
# between this script and run.sh and never becomes a job's exit code.
# Pending is true only of a file that is home: exit it only after a
# preserve that verified at the door. A catalog that cannot answer while
# the file is not home is NOT_HOME_EXIT, which run.sh stashes and fails
# like any other registration failure (payload 0.21.6: 5,576 Perlmutter
# jobs of 2026-09-21 exited pending with no file anywhere).
PENDING_EXIT = 81
NOT_HOME_EXIT = 1
# The output dataset does not exist: it is created at submission
# (docs/RUCIO_REGISTRATION_CONTRACT.md § 2), so a job that finds none was
# not submitted through that path. A code between this script and run.sh,
# which records the registration failed; never a job's exit code.
NO_DATASET_EXIT = 84


# Define the metadata schema
METADATA_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "ePICRucioMetadataTags",
    "description": "Optimized metadata tags for ePIC Rucio datasets using searchable slugs.",
    "type": "object",
    "properties": {
        "software_release": {
            "type": "string",
            "description": "Container version tag (e.g. 26.03.0-stable, nightly, unstable, or default)",
            "pattern": "^([0-9]+\\.[0-9]+\\.[0-9]+-stable|nightly|unstable|default)$"
        },
        "requester_pwg": {
            "type": "string",
            "description": "PWG requesting the dataset.",
            "enum": [
                "edt",
                "inclusive",
                "jets_hf",
                "semi_inclusive",
                "ew_bsm",
                "other"
            ]
        },
        "q2_min_gev2": {
            "type": "number",
            "description": "Minimum Q2 value (GeV^2). Optional - not applicable to all datasets."
        },
        "q2_max_gev2": {
            "type": "number",
            "description": "Maximum Q2 value (GeV^2). Optional - not applicable to all datasets."
        },
        "electron_beam_energy_gev": {
            "type": "number",
            "description": "Electron beam energy (GeV)"
        },
        "ion_beam_energy_gev": {
            "type": "number",
            "description": "Ion/nucleus beam energy (GeV)"
        },
        "is_background_mixed": {
            "type": "boolean",
            "description": "True if the sample includes background mixing; false if it is a regular/pure signal sample."
        },
        "ion_species": {
            "type": "string",
            "description": "Ion species.",
            "enum": [
                "p",
                "Au197",
                "Cu63",
                "He3",
                "H2",
                "Ru96",
                "Pb208",
                "Pb207"                
            ]
        },
        "data_level": {
            "type": "string",
            "description": "Data processing level.",
            "enum": [
                "simulation",
                "reconstruction"
            ]
        },
        "gun_particle": {
            "type": "string",
            "description": "Single particle type. Optional - only applicable to single particle datasets.",
            "enum": [
                "e-",
                "e+",
                "proton",
                "neutron",
                "pi+",
                "pi-",
                "pi0",
                "kaon-",
                "kaon+",
                "gamma",
                "mu-"
            ]
        },
        "geometry_config": {
            "type": "string",
            "description": "Geometry configuration tag (e.g. craterlake_18x275, craterlake_5x41_He3)",
            "pattern": "^[a-z][a-z0-9_]*_[0-9]+x[0-9]+(_.+)?$"
        },
        "gun_momentum_min_gev": {
            "type": "number",
            "description": "Minimum particle gun momentum (GeV). For fixed-energy runs, equals gun_momentum_max_gev."
        },
        "gun_momentum_max_gev": {
            "type": "number",
            "description": "Maximum particle gun momentum (GeV). For fixed-energy runs, equals gun_momentum_min_gev."
        },
        "gun_theta_min_deg": {
            "type": "number",
            "description": "Minimum polar angle (degrees) for particle gun angular distribution."
        },
        "gun_theta_max_deg": {
            "type": "number",
            "description": "Maximum polar angle (degrees) for particle gun angular distribution."
        },
        "gun_phi_min_deg": {
            "type": "number",
            "description": "Minimum azimuthal angle (degrees) for particle gun distribution. Default is 0."
        },
        "gun_phi_max_deg": {
            "type": "number",
            "description": "Maximum azimuthal angle (degrees) for particle gun distribution. Default is 360."
        },
        "gun_distribution": {
            "type": "string",
            "description": "Angular distribution type for particle gun.",
            "enum": ["uniform", "cos(theta)", "eta", "pseudorapidity", "ffbar"]
        },
        "requester_dsc": {
            "type": "string",
            "description": "Detector Subsystem Collaboration requesting the dataset. Optional.",
            "enum": [
                "tracking",
                "other"
            ]
        },
        "generator": {
            "type": "string",
            "description": "Generator name",
            "enum": [
                "pythia6",
                "pythia8",
                "beagle",
                "djangoh",
                "rapgap",
                "dempgen",
                "sartre",
                "lager",
                "estarlight",
                "epic",
                "getalm",
                "eicmesonsfgen",
                "eic_sr_geant4",
                "eic_esr_xsuite",
                "sherpa",
                "single_particle",
                "other"
            ]
        },
    },
    "required": [
        "software_release",
        "is_background_mixed",
        "data_level",
        "geometry_config",
        "generator"
    ]
}


def validate_metadata(metadata: Dict[str, Any]) -> bool:
    """
    Validate metadata against the schema using jsonschema.
    
    Parameters
    ----------
    metadata : dict
        The metadata dictionary to validate
        
    Returns
    -------
    bool
        True if valid
        
    Raises
    ------
    ValueError
        If metadata doesn't match the schema
    """
    if not isinstance(metadata, dict):
        raise ValueError("Metadata must be a JSON object (dictionary)")
    
    try:
        json_validate(instance=metadata, schema=METADATA_SCHEMA)
    except ValidationError as e:
        raise ValueError(f"Metadata validation failed: {e.message}")
    
    return True


def load_metadata_file(filepath: str) -> Dict[str, Any]:
    """
    Load and validate metadata from a JSON file.
    
    Parameters
    ----------
    filepath : str
        Path to the metadata JSON file
        
    Returns
    -------
    dict
        The validated metadata dictionary
        
    Raises
    ------
    FileNotFoundError
        If the metadata file doesn't exist
    ValueError
        If the JSON is invalid or doesn't match the schema
    """
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Metadata file not found: {filepath}")
    
    try:
        with open(filepath, 'r') as f:
            metadata = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in metadata file: {e}")
    
    validate_metadata(metadata)
    return metadata


# The output name forms the payload registers, so a derived name keeps the
# form: the attempt's mark goes before the level suffix, never after .root.
OUTPUT_SUFFIXES = ('.eicrecon.edm4eic.root', '.edm4hep.root', '.hepmc3.tree.root')


def derived_did_name(did_name: str, mark: str) -> str:
    """``<name>.p<mark>.<level suffix>``: the original name carrying the
    attempt's own mark, for an output whose name another attempt's
    different content already holds (docs/EPICPROD_PAYLOAD.md)."""
    for suffix in OUTPUT_SUFFIXES:
        if did_name.endswith(suffix):
            return f"{did_name[:-len(suffix)]}.p{mark}{suffix}"
    return f"{did_name}.p{mark}"


def _register_diverted(client, scope, upload_items, args):
    """Register the job's validated outputs under derived names, in their
    own datasets, when the names they owe hold different content. The
    mark is the PanDA job id (``PANDAID``), the attempt's own identity.
    Returns the derived DIDs, comma-joined, or '' when nothing could be
    registered; the divergence is a person's to resolve through content
    validation, so nothing here fails the job."""
    mark = str(os.environ.get('PANDAID') or '').strip() or datetime_mark()
    diverted = []
    for item in upload_items:
        derived = dict(item, did_name=derived_did_name(item['did_name'], mark))
        derived.pop('dataset_meta', None)
        derived.pop('lifetime', None)
        try:
            UploadClient(logger=logging.getLogger('upload_client')).upload([derived])
        except Exception as exc:  # noqa: BLE001
            logging.getLogger('upload_client').error(
                "diverted registration of %s:%s failed: %s", scope, derived['did_name'], exc)
            continue
        if args.events is not None:
            try:
                client.set_metadata(scope, derived['did_name'], 'events', int(args.events))
            except Exception as exc:  # noqa: BLE001
                logging.getLogger('upload_client').error(
                    "events not set on diverted %s:%s: %s", scope, derived['did_name'], exc)
        logging.getLogger('upload_client').warning(
            "output %s:%s holds different content; this attempt's output registered as %s:%s",
            scope, item['did_name'], scope, derived['did_name'])
        diverted.append(derived['did_name'])
    return ','.join(diverted)


def after_registration(client, scope, upload_items, args, dataset_meta, logger, noregister=False,
                       events_done=False):
    """What follows a registration whichever way it was made: the
    lifetime on a canary run's DIDs, the event count on each file
    (unless the registration already wrote it), and the comparison of
    the dataset's declared metadata with the job's own reading.
    Reported, never a failure: the registration stands.

    The dataset's derived event total is not read back here: that read
    listed every file of the output dataset on every job (50,000 rows a
    job on a 50,000-job task, held on a server thread each time), and it
    belongs once per task in the lineage sweep (payload 0.19.1)."""
    if args.lifetime and not noregister:
        # Every DID registered expires with the run's lifetime, the
        # files and their dataset alike, so the catalog forgets a canary
        # run as its replica is reaped. A failure here is logged, never
        # a failed job: the upload stands and the cleanup is by hand.
        for item in upload_items:
            for name in (item['did_name'], item['dataset_name']):
                try:
                    client.set_metadata(scope, name, 'lifetime', int(args.lifetime))
                except Exception as exc:  # noqa: BLE001
                    logger.error("lifetime not set on %s:%s: %s", scope, name, exc)
    if args.events is not None and not noregister and not events_done:
        # The event count on every file DID, from which Rucio derives
        # the dataset's total (RUCIO_REGISTRATION_CONTRACT.md). A count
        # that cannot be written is reported by name, and the upload stands.
        for item in upload_items:
            try:
                client.set_metadata(scope, item['did_name'], 'events', int(args.events))
                logger.info("events %d registered on %s:%s", int(args.events), scope, item['did_name'])
            except Exception as exc:  # noqa: BLE001
                logger.error("events not set on %s:%s: %s", scope, item['did_name'], exc)
    if dataset_meta and not noregister:
        # The dataset carries what the task declared at submission; the
        # job reports whether what it reads from its own output file
        # agrees. A difference or an absent value is reported, never a
        # failure (docs/RUCIO_REGISTRATION_CONTRACT.md § 2). To a file,
        # never to stdout, which the monitor shares.
        comparison = {}
        for ds_name in sorted({item['dataset_name'] for item in upload_items}):
            try:
                declared = client.get_metadata(scope, ds_name, plugin='ALL')
            except Exception as exc:  # noqa: BLE001
                # A dataset with no metadata of its own (a canary's flat
                # dataset) answers "no metadata found": it declares
                # nothing, which is a reading, not an error.
                if 'no metadata found' in str(exc).lower():
                    comparison[ds_name] = {'declared': False, 'agree': 0, 'differ': {},
                                           'absent': sorted(dataset_meta)}
                    logger.info("dataset %s:%s declares no metadata; the job read %d keys",
                                scope, ds_name, len(dataset_meta))
                    continue
                logger.error("metadata on dataset %s:%s unread: %s", scope, ds_name, exc)
                comparison[ds_name] = {'unread': str(exc)}
                continue
            differ = {k: {'dataset': declared.get(k), 'job': v} for k, v in dataset_meta.items()
                      if declared.get(k) is not None and declared.get(k) != v}
            absent = [k for k in dataset_meta if declared.get(k) is None]
            agree = len(dataset_meta) - len(differ) - len(absent)
            comparison[ds_name] = {'agree': agree, 'differ': differ, 'absent': absent}
            if differ or absent:
                logger.warning("metadata on dataset %s:%s: %d agree, differ %s, absent %s",
                               scope, ds_name, agree, differ or '{}', absent or '[]')
            else:
                logger.info("metadata on dataset %s:%s agrees with the job's reading (%d keys)",
                            scope, ds_name, agree)
        marker = os.environ.get('METADATA_COMPARE_OUT')
        if marker:
            try:
                with open(marker, 'w') as handle:
                    json.dump(comparison, handle)
            except OSError as exc:  # noqa: BLE001
                logger.error("metadata comparison not written: %s", exc)


class CatalogUnreachable(Exception):
    """The catalog of record did not answer; the file is home and the
    registration is owed (docs/RUCIO_RESILIENCE.md, Measure 2)."""


def local_adler32(path: str) -> str:
    import zlib
    value = 1
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(8 << 20), b''):
            value = zlib.adler32(chunk, value)
    return f'{value & 0xffffffff:08x}'


def _xrd(args, timeout):
    import subprocess
    return subprocess.run(['xrdfs'] + list(args), capture_output=True, text=True, timeout=timeout)


def stored_at(door: str, path: str, timeout: int = 120):
    """(bytes, adler32 or '') of the file at a door path, or None when it
    is not there. The storage is asked, never the catalog."""
    stat = _xrd([door, 'stat', path], timeout)
    if stat.returncode != 0:
        return None
    size = None
    for line in (stat.stdout or '').splitlines():
        if line.strip().startswith('Size:'):
            size = int(line.split(':', 1)[1].strip())
    if size is None:
        return None
    adler = ''
    check = _xrd([door, 'query', 'checksum', path], max(timeout, 300))
    if check.returncode == 0:
        parts = (check.stdout or '').split()
        if len(parts) >= 2 and parts[0].startswith('adler32'):
            adler = parts[1]
    return size, adler


def preserve(file_path: str, did_name: str, door: str, prefix: str, timeout: int, logger):
    """The first act with a finished output: copy it to its home at the RSE
    of record, the path the RSE's naming gives its logical name, with the
    job's own credential and no word to the catalog. Never overwrites: a
    file already at that path is an earlier attempt's, delivered or
    stashed, and this attempt's bytes go under the derived name instead
    (docs/RUCIO_RESILIENCE.md, Measure 3). Verified at the door by size and
    checksum against the local file. Returns (did registered under, size,
    adler32) or None when the copy could not be made or verified."""
    import subprocess
    size = os.path.getsize(file_path)
    adler = local_adler32(file_path)
    mark = str(os.environ.get('PANDAID') or '').strip() or datetime_mark()
    for name in (did_name, derived_did_name(did_name, mark)):
        path = f"{prefix.rstrip('/')}/{name.lstrip('/')}"
        try:
            existing = stored_at(door, path)
        except Exception as exc:  # noqa: BLE001
            logger.error("preserve: the door did not answer for %s: %s", path, exc)
            return None
        if existing is not None:
            if name == did_name:
                logger.warning("preserve: %s already holds a file (%s bytes); this attempt's "
                               "output goes under its derived name", path, existing[0])
                continue
            logger.error("preserve: %s already holds a file too; not overwriting", path)
            return None
        try:
            copy = subprocess.run(['xrdcp', file_path, f'{door}/{path}'],
                                  capture_output=True, text=True, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            logger.error("preserve: xrdcp to %s/%s failed: %s", door, path, exc)
            return None
        if copy.returncode != 0:
            logger.error("preserve: xrdcp to %s/%s exited %s: %s", door, path, copy.returncode,
                         (copy.stderr or '').strip()[-400:])
            return None
        try:
            stored = stored_at(door, path)
        except Exception as exc:  # noqa: BLE001
            stored = None
            logger.error("preserve: verification of %s failed: %s", path, exc)
        if stored is None or stored[0] != size or (stored[1] and stored[1] != adler):
            logger.error("preserve: %s does not verify (door %s, local %s bytes adler32 %s)",
                         path, stored, size, adler)
            return None
        logger.info("preserved %s at %s/%s: %s bytes, adler32 %s", did_name, door, path, size, adler)
        return name, size, adler
    return None


def register_in_place(client, scope, did_name, rse, size, adler, events, logger):
    """Register a file that already lies at its deterministic path on the
    RSE: the replica with the size and checksum verified at the door, the
    attachment to its dataset, the event count. Every catalog call that
    does not answer raises CatalogUnreachable: the file is home, the
    registrar completes the entry later. Returns 'registered' or
    'adopted' (an available replica of the same work was already there)."""
    dataset = did_name.rsplit('/', 1)[0]
    try:
        replicas = list(client.list_replicas([{'scope': scope, 'name': did_name}], all_states=True))
    except Exception as exc:  # noqa: BLE001
        raise CatalogUnreachable(f'list_replicas {scope}:{did_name}: {exc}')
    for replica in replicas:
        if 'AVAILABLE' in (replica.get('states') or {}).values():
            logger.warning("%s:%s is already registered with an available replica: adopted", scope, did_name)
            return 'adopted'
    entry = {'scope': scope, 'name': did_name, 'bytes': int(size), 'adler32': adler}
    # One call registers the replica at the RSE and attaches the file to
    # its dataset, committed or rolled back together (the pilot's own
    # stage-out call; Anil Panta, swf-epicprod PR #1): no orphaned replica,
    # and one request where there were two.
    try:
        client.add_files_to_datasets(
            [{'scope': scope, 'name': dataset, 'rse': rse, 'dids': [entry]}],
            ignore_duplicate=True)
    except Exception as exc:  # noqa: BLE001
        text = str(exc).lower()
        if not ('already' in text or 'duplicate' in text):
            raise CatalogUnreachable(f'add_files_to_datasets {scope}:{did_name} at {rse}: {exc}')
    if events is not None:
        try:
            client.set_metadata(scope, did_name, 'events', int(events))
        except Exception as exc:  # noqa: BLE001
            raise CatalogUnreachable(f'set_metadata events on {scope}:{did_name}: {exc}')
    logger.info("registered %s:%s in place at %s: %s bytes, adler32 %s", scope, did_name, rse, size, adler)
    return 'registered'


def _write_marker(env_name: str, content: str, logger) -> None:
    """A result for the run script, to a file named by the environment,
    never to stdout, which the monitor shares."""
    marker = os.environ.get(env_name)
    if not marker or not content:
        return
    try:
        with open(marker, 'w') as handle:
            handle.write(content)
    except OSError as exc:  # noqa: BLE001
        logger.error("%s marker not written: %s", env_name, exc)


def datetime_mark() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog='Register to RUCIO',
        description='Registers files to RUCIO with optional dataset metadata'
    )
    parser.add_argument(
        "-f", dest="file_paths",
        action="store", nargs='+', required=True,
        help="Enter the local file path(s)"
    )
    parser.add_argument(
        "-d", dest="did_names",
        action="store", nargs='+', required=True,
        help="Enter the data identifier(s) for rucio catalogue"
    )
    parser.add_argument(
        "-s", dest="scope",
        action="store", required=True,
        help="Enter the scope"
    )
    parser.add_argument(
        "-r", dest="rse",
        action="store", required=True,
        help="Enter the rucio storage element (e.g., EIC-XRD for production outputs)"
    )
    parser.add_argument(
        '--noregister', dest="noregister",
        action="store_true", default=False,
        help="Skip rucio registration (upload only)"
    )
    parser.add_argument(
        '--upload-metadata', dest="metadata_file",
        action="store", default=None,
        help="Path to JSON file containing dataset metadata"
    )
    parser.add_argument(
        '--metadata-json', dest="metadata_json",
        action="store", default=None,
        help="JSON string containing dataset metadata"
    )
    parser.add_argument(
        '--lifetime', dest="lifetime", type=int, default=None,
        help="Seconds the registration lives: the rule of a dataset this "
             "upload creates, and the expiry of every DID registered, so a "
             "canary run's output removes itself (epicprod payload canary)"
    )
    parser.add_argument(
        '--preserve-door', dest="preserve_door", default=None,
        help="Preserve first: the xrootd door of the RSE (root://host:port). "
             "The output is copied to its deterministic path there before "
             "the catalog is asked anything, and registered in place; a "
             "catalog that does not answer leaves the file home and the "
             "registration owed (docs/RUCIO_RESILIENCE.md, Measure 2)"
    )
    parser.add_argument(
        '--preserve-prefix', dest="preserve_prefix", default='/eic/EPIC',
        help="The RSE's path prefix under the door (default /eic/EPIC)"
    )
    parser.add_argument(
        '--preserve-timeout', dest="preserve_timeout", type=int, default=600,
        help="Seconds allowed for the copy home (default 600)"
    )
    parser.add_argument(
        '--events', dest="events", type=int, default=None,
        help="Event count of the file(s), written as Rucio's events "
             "attribute on each file DID after the upload and read back "
             "through the dataset's derived total "
             "(RUCIO_REGISTRATION_CONTRACT.md)"
    )

    args = parser.parse_args()

    file_paths = args.file_paths
    did_names = args.did_names
    scope = args.scope
    rse = args.rse
    noregister = args.noregister

    # Validation to ensure file_paths and did_names have the same length
    if len(file_paths) != len(did_names):
        raise ValueError("The number of file paths must match the number of did names.")

    # Validate that all files exist
    for file_path in file_paths:
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

    # Load and validate metadata if provided
    if args.metadata_file and args.metadata_json:
        raise ValueError("Cannot specify both --upload-metadata and --metadata-json")
    dataset_meta = None
    if args.metadata_file:
        dataset_meta = load_metadata_file(args.metadata_file)
        print(f"Loaded metadata: {json.dumps(dataset_meta, indent=2)}")
    elif args.metadata_json:
        try:
            dataset_meta = json.loads(args.metadata_json)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in --metadata-json: {e}")
        validate_metadata(dataset_meta)
        print(f"Loaded metadata: {json.dumps(dataset_meta, indent=2)}")

    upload_items = []  # List to hold the upload items
    client = Client()

    def _dataset_exists(name):
        from rucio.common.exception import DataIdentifierNotFound
        try:
            client.get_did(scope, name)
            return True
        except DataIdentifierNotFound:
            return False

    # Loop through the file paths and did names
    for file_path, did_name in zip(file_paths, did_names):
        parent_directory = os.path.dirname(did_name)  # Get the parent directory from did_name

        # Validate that parent_directory is not empty
        if not parent_directory:
            raise ValueError(
                f"DID name '{did_name}' does not contain a parent directory. "
                "Expected format: 'parent/filename'"
            )

        # Create a new dictionary for each file and did_name
        upload_item = {
            'path': file_path,
            'rse': rse,
            'did_scope': scope,
            'did_name': did_name,
            'dataset_scope': scope,
            'dataset_name': parent_directory,
            'no_register': noregister
        }

        # The dataset, its rule and its metadata exist before the task is
        # submitted (docs/RUCIO_REGISTRATION_CONTRACT.md § 2); the upload
        # attaches the file and carries no dataset metadata and no lifetime.
        # The catalog is asked once per dataset: absent is a failure of the
        # submission path, not of this job's work, and no bytes move for it.
        if not noregister and not args.preserve_door:
            try:
                present = _dataset_exists(parent_directory)
            except Exception as exc:  # noqa: BLE001
                print(f"Catalog unreachable while asking for dataset {scope}:{parent_directory}: {exc}; "
                      f"no byte has moved, so the output is not home and is handed to the stash.",
                      file=sys.stderr)
                sys.exit(NOT_HOME_EXIT)
            if not present:
                print(f"ERROR: output dataset {scope}:{parent_directory} does not exist; it is created "
                      f"at submission (RUCIO_REGISTRATION_CONTRACT.md § 2), so this job's output "
                      f"cannot be registered.", file=sys.stderr)
                sys.exit(NO_DATASET_EXIT)

        # Append the new item to the upload_items list
        upload_items.append(upload_item)

    # Set up logging
    logger = logging.getLogger('upload_client')
    logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.INFO)

    if args.preserve_door and not noregister:
        # Preserve first (docs/RUCIO_RESILIENCE.md, Measure 2 as built,
        # payload 0.19.0): every output is copied to its home at the RSE
        # before the catalog is asked anything. A copy that cannot be made
        # or verified falls back to the upload client below, the path that
        # held before; once home, no catalog failure costs the file.
        homed = []
        for item in upload_items:
            home = preserve(item['path'], item['did_name'], args.preserve_door,
                            args.preserve_prefix, args.preserve_timeout, logger)
            if home is None:
                break
            homed.append((item, home))
        if len(homed) == len(upload_items):
            registered_as = []
            try:
                for item, (name, size, adler) in homed:
                    if not _dataset_exists(item['dataset_name']):
                        print(f"ERROR: output dataset {scope}:{item['dataset_name']} does not exist; "
                              f"it is created at submission (RUCIO_REGISTRATION_CONTRACT.md § 2). "
                              f"The output is home at {args.preserve_door} {args.preserve_prefix}/{name}",
                              file=sys.stderr)
                        sys.exit(NO_DATASET_EXIT)
                    register_in_place(client, scope, name, rse, size, adler, args.events, logger)
                    registered_as.append(name)
            except CatalogUnreachable as exc:
                # The file is home; only the catalog entry is owed.
                diverted = ','.join(n for (item, (n, _s, _a)) in homed if n != item['did_name'])
                _write_marker('DIVERTED_OUT', diverted, logger)
                print(f"Catalog unreachable after the output was preserved: {exc}. "
                      f"Exiting pending; the file is home and the registrar completes the registration.",
                      file=sys.stderr)
                sys.exit(PENDING_EXIT)
            except SystemExit:
                raise
            except Exception as exc:  # noqa: BLE001
                _write_marker('DIVERTED_OUT',
                              ','.join(n for (item, (n, _s, _a)) in homed if n != item['did_name']), logger)
                print(f"Registration in place failed after the output was preserved: {exc}. "
                      f"Exiting pending; the file is home and the registrar completes the registration.",
                      file=sys.stderr)
                sys.exit(PENDING_EXIT)
            items_as_registered = [dict(item, did_name=name) for item, (name, _s, _a) in homed]
            try:
                after_registration(client, scope, items_as_registered, args, dataset_meta, logger, noregister,
                                   events_done=True)
            except Exception as exc:  # noqa: BLE001
                # The registration stands; what follows it is reported, never a failure.
                logger.error("after the registration: %s", exc)
            diverted = ','.join(n for (item, (n, _s, _a)) in homed if n != item['did_name'])
            if diverted:
                logger.warning("this attempt's output registered under a derived name: %s", diverted)
                _write_marker('DIVERTED_OUT', diverted, logger)
            sys.exit(0)
        logger.error("preserve-first could not home every output; falling back to the upload client")

    upload_client = UploadClient(logger=logger)

    try:
        upload_client.upload(upload_items)
        logger.info("Upload completed successfully!")
        after_registration(client, scope, upload_items, args, dataset_meta, logger, noregister)
    except Exception as e:
        logger.error(f"Upload failed: {e}")

        dids = [{'scope': scope, 'name': did_name} for did_name in did_names]

        # A DID already registered with an available replica anywhere is the
        # output of an earlier attempt of this job that delivered before the
        # job was counted failed. That is delivery, not a failure: the file
        # passed the same validation before its registration, and a retry's
        # bytes differ only because the simulation is not reproducible. Exit
        # success rather than fail the job on the conflict (epicprod payload,
        # swf-epicprod docs/EPICPROD_PAYLOAD.md).
        # Asking the catalog is itself a call on the thing that just failed.
        # When it cannot answer, the job does not know whether an earlier
        # attempt delivered, and this attempt's upload failed, so its file is
        # not home: never pending here (a pending exit with no file anywhere
        # loses the physics silently). Hand the output to the stash.
        try:
            delivered = [
                rep['name'] for rep in client.list_replicas(dids, all_states=True)
                if 'AVAILABLE' in (rep.get('states') or {}).values()]
        except Exception as probe:                            # noqa: BLE001
            logger.error(
                "Catalog unreachable while asking whether the output is "
                "delivered: %s. The upload failed, so the output is not home; "
                "handing it to the stash.", probe)
            sys.exit(NOT_HOME_EXIT)
        if delivered and len(delivered) == len(dids):
            # Adopt, but only what is the same work. An available replica
            # under this name carrying a different event count is other
            # content, and adopting it would report someone else's file as
            # this job's delivery (docs/RUCIO_RESILIENCE.md, Measure 3).
            same = True
            if args.events is not None:
                for did_name in did_names:
                    try:
                        recorded = client.get_metadata(scope, did_name).get('events')
                    except Exception as exc:  # noqa: BLE001
                        logger.error("events unreadable on %s:%s: %s",
                                     scope, did_name, exc)
                        recorded = None
                    if recorded is None or int(recorded) != int(args.events):
                        logger.warning(
                            "%s:%s carries %s events, this job made %s: not the "
                            "same work", scope, did_name, recorded, args.events)
                        same = False
                        break
            if same:
                logger.warning(
                    "Output already registered with an available replica, "
                    "delivered by an earlier attempt: %s", delivered)
                sys.exit(0)
            # Different content under the name we owe. Validated data is
            # never discarded to protect a naming rule: it registers under a
            # derived name and the divergence is a human's to resolve
            # through content validation.
            diverted = _register_diverted(client, scope, upload_items, args)
            if diverted:
                # To a file, never to stdout: this script runs under prmon
                # and a caller reading its output is the trap that once
                # polluted the metadata JSON and killed a canary.
                marker = os.environ.get('DIVERTED_OUT')
                if marker:
                    try:
                        with open(marker, 'w') as handle:
                            handle.write(diverted)
                    except OSError as exc:  # noqa: BLE001
                        logger.error("diverted marker not written: %s", exc)
                sys.exit(0)
            logger.error("the output name holds different content and the "
                         "derived name could not be registered either")

        # Get replicas for all DIDs in the rse
        try:
            replicas = client.list_replicas(
                dids,
                all_states=True,
                rse_expression=rse
            )
        except Exception as probe:                            # noqa: BLE001
            logger.error(
                "Catalog unreachable while listing replicas at %s: %s. "
                "The upload failed, so the output is not home; handing it "
                "to the stash.", rse, probe)
            sys.exit(NOT_HOME_EXIT)

        # Collect files that need to be cleaned up
        files_to_update = []
        files_to_tombstone = []
        
        for replica in replicas:
            did_name = replica['name']
            state = replica['states'].get(rse)
            
            if state == 'COPYING':
                logger.warning(
                    "Found COPYING replica %s:%s on %s — deleting",
                    scope, did_name, rse
                )
                files_to_update.append({'scope': scope, 'name': did_name, 'state': 'U'})
                files_to_tombstone.append({'rse': rse, 'scope': scope, 'name': did_name})
        
        if files_to_update:
            # Update replica states to UNAVAILABLE(U)
            client.update_replicas_states(rse=rse, files=files_to_update)
            # set tombstone to that did, should trigger deletion
            client.set_tombstone(files_to_tombstone)
        
        raise
