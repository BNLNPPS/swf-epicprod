"""The output datasets of a submission and their metadata, composed from
the EVGEN spec before the task is submitted
(docs/RUCIO_REGISTRATION_CONTRACT.md § 2).

The names follow the payload's naming contract (``run.sh``): the tag is
``DETECTOR_VERSION/DETECTOR_CONFIG[/TAG_PREFIX]/<EVGEN-relative dir>``
and the datasets are ``/FULL/<tag>``, ``/RECO/<tag>`` and, for an
internal-EVGEN task that keeps its sample, ``/EVGEN/<dir>``; a trial's
sit under its output root. The metadata is what the job reads back from
its own output file (``parse_podio_metadata.py``, ``eic-info``),
composed here from the same inputs with the payload's own detectors, so
the job's comparison is like against like.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from swf_epicprod.payload.shared_utils import (
    detect_dsc,
    detect_generator,
    detect_pwg,
    detect_q2,
)

HEPMC_EXT = "hepmc3.tree.root"
LEVEL_DATA = {"FULL": "simulation", "RECO": "reconstruction"}


def manifest_row(row: str) -> tuple[str, str, str, str]:
    parts = [p.strip() for p in str(row).split(",")]
    if len(parts) < 4:
        raise ValueError(f"EVGEN manifest row has fewer than 4 fields: {row!r}")
    return parts[0], parts[1], parts[2], parts[3]


def evgen_dir(file_col: str) -> str:
    """The EVGEN-relative directory of a manifest row: the tag's tail
    and the generated sample's dataset."""
    parent = str(PurePosixPath(file_col).parent)
    return "" if parent == "." else parent.strip("/")


def output_tag(env: dict[str, Any], file_col: str) -> str:
    parts = [str(env.get("DETECTOR_VERSION") or "main"), str(env.get("DETECTOR_CONFIG") or "")]
    prefix = str(env.get("TAG_PREFIX") or "").strip("/")
    if prefix:
        parts.append(prefix)
    tail = evgen_dir(file_col)
    if tail:
        parts.append(tail)
    return "/".join(p for p in parts if p)


def software_release(cfg: dict[str, Any]) -> str:
    """The release the job reports from ``eic-info`` (``jug_dev: <tag>``),
    read here from the configuration's image tag with the same reading
    ``run.sh`` applies: ``26.07.1-stable`` stays, a bare channel name
    (nightly, stable, unstable, default) is the channel."""
    tag = str(cfg.get("jug_xl_tag") or "")
    if not tag:
        image = str(cfg.get("container_image") or "")
        tag = image.rsplit(":", 1)[1] if ":" in image else ""
    m = re.search(r"\d[\d.]*-(?=stable)stable", tag)
    if m:
        return m.group(0)
    m = re.search(r"(stable|unstable|nightly|default)", tag)
    return m.group(1) if m else tag


def geometry_config(env: dict[str, Any]) -> str:
    """The compact file's basename without ``epic_``, as the job reads
    it from the FULL file: ``DETECTOR_CONFIG[_<beams>]`` where the beams
    are ``DETECTOR_BEAMS`` or ``EBEAMxPBEAM`` (run.sh, the geometry stage)."""
    config = str(env.get("DETECTOR_CONFIG") or "")
    beams = str(env.get("DETECTOR_BEAMS") or "")
    if not beams and env.get("EBEAM") and env.get("PBEAM"):
        beams = f"{env['EBEAM']}x{env['PBEAM']}"
    name = f"{config}_{beams}" if beams else config
    return name[len("epic_"):] if name.startswith("epic_") else name


def dataset_metadata(spec: dict[str, Any], cfg: dict[str, Any], level: str, file_col: str, ext: str) -> dict[str, Any]:
    """The dataset metadata for one output level of the rows under
    ``file_col``'s directory, as ``parse_podio_metadata.py`` would read it
    from the produced file; ``level`` is ``FULL``, ``RECO`` or ``EVGEN``."""
    env = spec.get("env") or {}
    path = f"EVGEN/{file_col}.{ext}"
    is_single = ext != HEPMC_EXT
    no_beam = "BACKGROUNDS" in f"EVGEN/{file_col}"
    bg_mixed = bool(str(env.get("BG_FILES") or "").strip())
    md: dict[str, Any] = {
        "software_release": software_release(cfg),
        "generator": detect_generator(path, is_single=is_single),
        "is_background_mixed": bg_mixed,
    }
    if not is_single and not no_beam:
        md["requester_pwg"] = detect_pwg(path)
    q2_min, q2_max = detect_q2(path)
    if q2_min is not None:
        md["q2_min_gev2"] = q2_min
    if q2_max is not None:
        md["q2_max_gev2"] = q2_max
    dsc = detect_dsc(path, is_background_mixed=bg_mixed)
    if dsc:
        md["requester_dsc"] = dsc
    if level in LEVEL_DATA:
        md["data_level"] = LEVEL_DATA[level]
    geometry = geometry_config(env)
    if geometry:
        md["geometry_config"] = geometry
        if not no_beam and not is_single:
            m = re.search(r"_(\d+)x(\d+)(?:_(.+))?$", geometry)
            if m:
                md["electron_beam_energy_gev"] = int(m.group(1))
                md["ion_beam_energy_gev"] = int(m.group(2))
                md["ion_species"] = m.group(3) if m.group(3) is not None else "p"
    # A single-particle sample's gun parameters are in its steering file,
    # read by the job; they are not declared here.
    return md


def output_datasets(spec: dict[str, Any], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Every dataset the submission's jobs register into, with its
    metadata: one per output level and EVGEN directory of the manifest.
    Levels follow the environment: FULL when ``COPYFULL``, RECO when
    ``COPYRECO``, EVGEN when an internal-EVGEN task keeps its sample
    (``COPYEVGEN``). A trial's datasets sit under its output root."""
    env = spec.get("env") or {}
    on = lambda key: str(env.get(key, "")).lower() == "true"  # noqa: E731
    levels = [lvl for lvl, key in (("FULL", "COPYFULL"), ("RECO", "COPYRECO")) if on(key)]
    if on("EVGEN_INTERNAL") and on("COPYEVGEN"):
        levels.append("EVGEN")
    root = str(((spec.get("trial") or {}).get("outputRoot")) or "").strip("/")
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for row in spec.get("csvRows") or []:
        file_col, ext, _events, _chunk = manifest_row(row)
        for level in levels:
            key = (level, evgen_dir(file_col))
            if key in seen:
                continue
            tail = evgen_dir(file_col) if level == "EVGEN" else output_tag(env, file_col)
            name = "/".join(p for p in (root, level, tail) if p)
            # A generated EVGEN sample registers without dataset metadata,
            # as the job registers it (run.sh, the evgen registration); the
            # metadata schema describes produced FULL and RECO.
            seen[key] = {
                "level": level,
                "dataset": f"/{name}",
                "metadata": dataset_metadata(spec, cfg, level, file_col, ext) if level in LEVEL_DATA else None,
            }
    return list(seen.values())
