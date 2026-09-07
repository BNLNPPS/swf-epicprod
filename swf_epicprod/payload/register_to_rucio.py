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
PENDING_EXIT = 81


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

        # Add metadata if provided and not in noregister mode
        if dataset_meta and not noregister:
            upload_item['dataset_meta'] = dataset_meta
        # A lifetime bounds the rule of a dataset this upload creates; the
        # client refuses a lifetime on a dataset that already exists, so a
        # second file into the same expiring dataset carries none here and
        # gets its DID expiry below.
        if args.lifetime and not noregister and not _dataset_exists(parent_directory):
            upload_item['lifetime'] = int(args.lifetime)

        # Append the new item to the upload_items list
        upload_items.append(upload_item)

    # Set up logging
    logger = logging.getLogger('upload_client')
    logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.INFO)

    upload_client = UploadClient(logger=logger)

    try:
        upload_client.upload(upload_items)
        logger.info("Upload completed successfully!")
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
        if args.events is not None and not noregister:
            # The event count on every file DID, from which Rucio derives
            # the dataset's total (RUCIO_REGISTRATION_CONTRACT.md). The
            # derived total is read back and held to the sum of the
            # dataset's files; a count that cannot be written or does not
            # verify is reported by name, and the upload stands.
            for item in upload_items:
                try:
                    client.set_metadata(scope, item['did_name'], 'events', int(args.events))
                    logger.info("events %d registered on %s:%s", int(args.events), scope, item['did_name'])
                except Exception as exc:  # noqa: BLE001
                    logger.error("events not set on %s:%s: %s", scope, item['did_name'], exc)
            for ds_name in sorted({item['dataset_name'] for item in upload_items}):
                try:
                    counts = [f.get('events') for f in client.list_files(scope, ds_name)]
                    derived = client.get_metadata(scope, ds_name).get('events')
                except Exception as exc:  # noqa: BLE001
                    logger.error("events on dataset %s:%s unverified: %s", scope, ds_name, exc)
                    continue
                if any(c is None for c in counts):
                    logger.error("events on dataset %s:%s unverified: %d of %d files carry no count",
                                 scope, ds_name, sum(1 for c in counts if c is None), len(counts))
                elif derived != sum(counts):
                    logger.error("events on dataset %s:%s unverified: derived %s differs from the sum of its files %d",
                                 scope, ds_name, derived, sum(counts))
                else:
                    logger.info("events on dataset %s:%s verified: %d over %d files", scope, ds_name, derived, len(counts))
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
        # When it cannot answer, the job knows neither that its output is
        # delivered nor that it is not, and failing on that ignorance is what
        # costs the finished physics: exit pending instead and let the
        # registrar settle it later (docs/RUCIO_RESILIENCE.md, Measure 2).
        try:
            delivered = [
                rep['name'] for rep in client.list_replicas(dids, all_states=True)
                if 'AVAILABLE' in (rep.get('states') or {}).values()]
        except Exception as probe:                            # noqa: BLE001
            logger.error(
                "Catalog unreachable while asking whether the output is "
                "delivered: %s. Exiting pending; the output stands and the "
                "registrar completes it.", probe)
            sys.exit(PENDING_EXIT)
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
                "Exiting pending rather than failing finished work.",
                rse, probe)
            sys.exit(PENDING_EXIT)

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
