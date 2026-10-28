"""JEDI's DDM interface for ePIC: inputs that reside in whichever Rucio
catalog holds them (docs/NERSC_PERLMUTTER.md, EVGEN input in production).

The upstream interface (``AtlasDDMClient``) builds one Rucio client from the
server's configuration, PanDA's own catalog, in every method. ePIC's inputs
reside wherever their catalog says: today the EVGEN samples are catalogued
by JLab's Rucio, under scope ``epic`` with path names (``/EVGEN/...``); no
copy needs to be registered in PanDA's catalog for a task to run on them.
This interface routes each DID the input-facing methods are given to the
catalog that holds it, per the catalog map, and leaves everything else (the
outputs, PanDA's own datasets) on the server's catalog as before.

A file such a catalog lists without a GUID, as JLab's EVGEN files are, is
given a stable one derived from its ``scope:name``: JEDI keys input files by
GUID, and the same file must have the same GUID in every task.

Registration, in ``panda_jedi.cfg`` ``[ddm]``::

    modConfig = wlcg:1:swf_epicprod.jedi.EpicDDMClient:EpicDDMClient,epic:1:swf_epicprod.jedi.EpicDDMClient:EpicDDMClient

The catalog map, ``EPIC_CATALOGS`` (default
``/opt/panda/etc/panda/epic_catalogs.json``, readable by the JEDI account
only, since it carries the catalogs' read credentials). Every client option
the catalog needs is named, ``vo`` above all: one left out falls back to the
server's own ``rucio.cfg`` under JEDI's ``VIRTUAL_ENV`` (``vo = eic``,
PanDA's catalog), which JLab's refuses as a failed authentication::

    {"catalogs": [{"name": "jlab", "scopes": ["epic"], "name_prefixes": ["/"], "lfn_basename": true,
                   "site_staged": ["NERSC_Perlmutter_epic_es"],
                   "client": {"rucio_host": ..., "auth_host": ..., "account": ...,
                              "auth_type": "userpass", "creds": {...}, "vo": "def",
                              "ca_cert": false}}]}
"""
import json
import os
import sys
import threading
import uuid
from contextlib import contextmanager

from pandajedi.jediddm import AtlasDDMClient as upstream

CONFIG = os.environ.get('EPIC_CATALOGS', '/opt/panda/etc/panda/epic_catalogs.json')
_local = threading.local()
_server_client = upstream.RucioClient
_cache = {'mtime': None, 'catalogs': []}
_lock = threading.Lock()


def catalogs():
    """The catalog map, reread when its file changes; empty without one."""
    try:
        mtime = os.path.getmtime(CONFIG)
    except OSError:
        return []
    with _lock:
        if _cache['mtime'] != mtime:
            with open(CONFIG) as stream:
                _cache['catalogs'] = json.load(stream).get('catalogs') or []
            _cache['mtime'] = mtime
        return _cache['catalogs']


def split_did(did):
    """``(scope, name)`` of ``scope:name``; a name may itself hold ':'-free
    path components (``epic:/EVGEN/...``)."""
    did = str(did or '').rstrip('/')
    if ':' in did:
        scope, name = did.split(':', 1)
        return scope, name
    return '', did


def catalog_for(did):
    """The catalog entry that holds ``did``, or None for the server's own."""
    scope, name = split_did(did)
    for entry in catalogs():
        if scope in (entry.get('scopes') or []) and any(
                name.startswith(p) for p in entry.get('name_prefixes') or ['']):
            return entry
    return None


def stable_guid(scope, name):
    """A GUID for a file its catalog lists without one, the same in every
    task: 32 hex digits from the file's ``scope:name``."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f'rucio:{scope}:{name}').hex


class CatalogClient:
    """A Rucio client of one catalog, as the upstream methods use it; files
    listed without a GUID are given their stable one."""

    def __getattr__(self, attr):
        return getattr(self._client, attr)

    def __init__(self, entry):
        self._client = _server_client(**entry['client'])
        self._basename = bool(entry.get('lfn_basename'))

    def list_files(self, scope, name, *args, **kwargs):
        for f in self._client.list_files(scope, name, *args, **kwargs):
            if not f.get('guid'):
                f = dict(f, guid=stable_guid(f.get('scope') or scope, f['name']))
            if self._basename:
                # A catalog whose names are paths (JLab's /EVGEN/...): JEDI
                # knows each file by its last component, unique in its
                # dataset, since the Event Service spec it builds for a job
                # separates its fields with '/' (es:<n>:<lfn>/<first>/<last>/...)
                # and a path in the LFN breaks it. The GUID stays the full
                # name's; the site copy is named by the same last component.
                f = dict(f, name=f['name'].rsplit('/', 1)[-1])
            yield f


def _client(*args, **kwargs):
    """The upstream module's one client constructor: the routed catalog's
    client inside ``routed``, the server's own everywhere else."""
    entry = getattr(_local, 'catalog', None)
    if entry is None:
        return _server_client(*args, **kwargs)
    return CatalogClient(entry)


upstream.RucioClient = _client


@contextmanager
def routed(did):
    previous = getattr(_local, 'catalog', None)
    _local.catalog = catalog_for(did)
    try:
        yield _local.catalog
    finally:
        _local.catalog = previous


TASK_FILE_EVENTS = 'epicFileEvents'


def task_file_events(depth=12):
    """The per-file event counts the task being read names, ``{lfn:
    events}`` in its parameters (``epicFileEvents``), else {}. JEDI's
    contents feeder lists a task's input through this interface and holds
    the task's parameters as ``taskParamMap`` in the calling frame; nothing
    else passes the task to a DDM interface."""
    frame = sys._getframe(1)
    for _ in range(depth):
        if frame is None:
            break
        params = frame.f_locals.get('taskParamMap')
        if isinstance(params, dict):
            return params.get(TASK_FILE_EVENTS) or {}
        frame = frame.f_back
    return {}


class EpicDDMClient(upstream.AtlasDDMClient):
    """The upstream interface with input-facing methods routed per DID."""

    def getFilesInDataset(self, datasetName, *args, **kwargs):
        with routed(datasetName):
            result = super().getFilesInDataset(datasetName, *args, **kwargs)
        events = task_file_events()
        if not events:
            return result
        # A task that names its files' event counts (an EVGEN task's manifest
        # covers its last file only part way): JEDI reads each file's count
        # from the listing (useRealNumEvents), so the listing carries the
        # task's counts in place of the catalog's full ones, and JEDI's total
        # (nEvents), their sum, takes every file (docs/NERSC_PERLMUTTER.md,
        # EVGEN input in production).
        status, files = result if isinstance(result, tuple) else (None, result)
        if isinstance(files, dict):
            for entry in files.values():
                if isinstance(entry, dict) and entry.get('lfn') in events:
                    entry['events'] = int(events[entry['lfn']])
        return result

    def getDatasetMetaData(self, datasetName, *args, **kwargs):
        with routed(datasetName):
            return super().getDatasetMetaData(datasetName, *args, **kwargs)

    def listDatasets(self, datasetName, *args, **kwargs):
        with routed(datasetName):
            return super().listDatasets(datasetName, *args, **kwargs)

    def listDatasetsInContainer(self, containerName, *args, **kwargs):
        with routed(containerName):
            return super().listDatasetsInContainer(containerName, *args, **kwargs)

    def expandContainer(self, containerName, *args, **kwargs):
        with routed(containerName):
            return super().expandContainer(containerName, *args, **kwargs)

    def listDatasetReplicas(self, datasetName, *args, **kwargs):
        with routed(datasetName):
            return super().listDatasetReplicas(datasetName, *args, **kwargs)

    def listReplicasPerDataset(self, datasetName, *args, **kwargs):
        with routed(datasetName):
            return super().listReplicasPerDataset(datasetName, *args, **kwargs)

    def getAvailableFiles(self, datasetSpec, *args, **kwargs):
        with routed(datasetSpec.datasetName) as entry:
            result = super().getAvailableFiles(datasetSpec, *args, **kwargs)
        staged = set((entry or {}).get('site_staged') or [])
        if not staged:
            return result
        # A queue that stages its own inputs ahead of their jobs (the map's
        # site_staged; Perlmutter's copy on its community file system, made
        # before the task is submitted, swf_epicprod/nersc_stage.py) holds
        # the dataset's files on its local disk: JEDI marks them ready and
        # the job is activated, as for a replica at the site's own storage.
        status, available = result if isinstance(result, tuple) else (None, result)
        if not isinstance(available, dict):
            return result
        for site in list(available) + [k for k in (args[0] if args else kwargs.get('site_endpoint_map') or {})
                                       if k not in available]:
            if site in staged:
                entry_map = available.setdefault(site, {'localdisk': [], 'localtape': [], 'cache': [], 'remote': []})
                entry_map['localdisk'] = list(datasetSpec.Files)
        return (status, available) if isinstance(result, tuple) else available

    def jedi_list_replicas_with_dataset(self, datasetName, *args, **kwargs):
        with routed(datasetName):
            return super().jedi_list_replicas_with_dataset(datasetName, *args, **kwargs)
