"""A background tag's sources JSON held in PCS (PCS_BACKGROUND_TAG.md, The
sources JSON is held in PCS): the contents of the ``config_data`` file a tag
names in ``bg_files``, stored on the tag as ``bg_files_content`` so that
submission needs no download from the campaign datasets repository.

Load the contents of every tag that names a file, or of one tag::

    python -m pcs.background_files [--tag k26] [--source-dir DIR] [--apply]

Without ``--apply`` it reads and validates only.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

CONTENT_KEY = 'bg_files_content'
# The campaign datasets repository's config_data, as the submit doer read it.
SOURCE_BASE = os.environ.get(
    'PCS_BG_CONFIG_BASE',
    'https://eicweb.phy.anl.gov/EIC/campaigns/datasets/-/raw/main/config_data')


def stored(params):
    """The stored sources of a tag's parameters when they belong to its
    current ``bg_files``, else None."""
    params = params or {}
    name = params.get('bg_files')
    content = params.get(CONTENT_KEY) or {}
    if name and content.get('file') == name and content.get('sources') is not None:
        return content['sources']
    return None


def read_sources(name, source_dir=None, timeout=30):
    """(sources, where) for the config_data file ``name``: from a checkout
    when ``source_dir`` is given, else from the eicweb mirror. Raises
    ValueError when the file is not valid JSON."""
    if source_dir:
        where = os.path.join(source_dir, os.path.basename(name))
        with open(where) as fh:
            text = fh.read()
    else:
        where = f"{SOURCE_BASE}/{urllib.parse.quote(os.path.basename(name), safe='')}"
        with urllib.request.urlopen(where, timeout=timeout) as reply:
            text = reply.read().decode()
    try:
        return json.loads(text), where
    except ValueError as e:
        raise ValueError(f'{name} from {where} is not JSON: {e}')


def load(tag, *, source_dir=None, changed_by='', apply=True):
    """Store the sources JSON of the file ``tag`` names on the tag; returns
    the stored content. Nothing is written without ``apply``."""
    from django.utils import timezone

    from monitor_app.epicprod_logging import log_epicprod_action

    from .permanence import identity_change

    name = (tag.parameters or {}).get('bg_files')
    if not name:
        raise ValueError(f'{tag.tag_label} names no bg_files')
    sources, where = read_sources(name, source_dir)
    content = {'file': name, 'sources': sources, 'loaded_from': where,
               'loaded_at': timezone.now().isoformat(), 'loaded_by': changed_by}
    if apply:
        tag.parameters = dict(tag.parameters or {}, **{CONTENT_KEY: content})
        with identity_change(changed_by, 'background sources JSON stored on the tag'):
            tag.save()
        log_epicprod_action(
            'pcs', 'tag_bg_files_loaded', outcome='ok', username=changed_by,
            subject_type='tag', subject_key=tag.tag_label,
            message=f'{tag.tag_label}: sources JSON {name} stored in PCS from {where}',
            bg_files=name)
    return content


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--tag', help='one tag label (k26, e95, ...); default every tag naming bg_files')
    ap.add_argument('--source-dir', help='a config_data checkout instead of the eicweb mirror')
    ap.add_argument('--apply', action='store_true', help='write the contents to the tags')
    ap.add_argument('--changed-by', default='pcs.background_files')
    args = ap.parse_args(argv)

    import django
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')
    django.setup()
    from .models import BackgroundTag, EvgenTag

    tags = [t for model in (BackgroundTag, EvgenTag) for t in model.objects.all()
            if (t.parameters or {}).get('bg_files')
            and (not args.tag or t.tag_label == args.tag)]
    failed = 0
    for tag in tags:
        if stored(tag.parameters) is not None:
            print(f'{tag.tag_label}: already stored ({tag.parameters["bg_files"]})')
            continue
        try:
            content = load(tag, source_dir=args.source_dir, changed_by=args.changed_by,
                           apply=args.apply)
            print(f"{tag.tag_label}: {'stored' if args.apply else 'read'} "
                  f"{content['file']} ({len(json.dumps(content['sources']))} bytes)")
        except Exception as e:                                # noqa: BLE001
            failed += 1
            print(f'{tag.tag_label}: FAILED {e}', file=sys.stderr)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
