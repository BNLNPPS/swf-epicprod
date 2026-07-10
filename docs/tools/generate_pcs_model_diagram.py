#!/usr/bin/env python3
"""Generate the PCS data-model diagram (Graphviz DOT to stdout).

Renders the production configuration data model from the live Django
models — entities, relations, and every field — so the diagram is
verifiable against the code and complete.

Usage::

    python docs/tools/generate_pcs_model_diagram.py > pcs_data_model.dot
    dot -Tsvg pcs_data_model.dot -o pcs_data_model.svg
"""
import os
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'swf_monitor_project.settings')
import django  # noqa: E402
django.setup()

from django.apps import apps  # noqa: E402

GROUPS = {
    'tags': ['PhysicsCategory', 'PhysicsTag', 'EvgenTag', 'SimuTag',
             'RecoTag', 'BackgroundTag'],
    'catalog': ['Dataset', 'Campaign'],
    'production': ['ProdConfig', 'ProdRequest', 'ProdTask', 'Questionnaire',
                   'PandaTasks'],
}
GROUP_STYLE = {
    'tags': ('#eef4fb', 'Configuration tags'),
    'catalog': ('#eaf6ec', 'Datasets and campaigns'),
    'production': ('#fdf3e7', 'Requests and tasks'),
}


def node(model):
    name = model.__name__
    rows = []
    for f in model._meta.get_fields():
        if f.auto_created:
            continue
        if f.is_relation and f.many_to_one:
            rows.append(f'<tr><td align="left">{f.name}</td>'
                        f'<td align="left"><font color="#666666">→ '
                        f'{f.related_model.__name__}</font></td></tr>')
        elif hasattr(f, 'attname') and not f.is_relation:
            rows.append(f'<tr><td align="left">{f.name}</td>'
                        f'<td align="left"><font color="#666666">'
                        f'{f.get_internal_type().replace("Field","").lower()}'
                        f'</font></td></tr>')
    label = (f'<<table border="0" cellborder="0" cellspacing="0" '
             f'cellpadding="2"><tr><td colspan="2" align="left">'
             f'<b>{name}</b></td></tr><hr/>{"".join(rows)}</table>>')
    return f'  {name} [label={label}];'


def main():
    pcs = apps.get_app_config('pcs')
    models = {m.__name__: m for m in pcs.get_models()}
    print('digraph pcs_data_model {')
    print('  rankdir=TB;')
    print('  graph [fontname="Helvetica", fontsize=18, ranksep=0.9, '
          'nodesep=0.45];')
    print('  node [shape=box, style="rounded,filled", '
          'fontname="Helvetica", fontsize=15, margin=0.12];')
    print('  edge [fontname="Helvetica", fontsize=14, color="#444444"];')
    for group, names in GROUPS.items():
        fill, label = GROUP_STYLE[group]
        print(f'  subgraph cluster_{group} {{')
        print(f'    label="{label}"; fontsize=20; style="rounded,filled"; '
              f'fillcolor="{fill}"; color="#c8c8c8";')
        print(f'    node [fillcolor="white"];')
        for n in names:
            if n in models:
                print('  ' + node(models[n]))
        print('  }')
    # relation edges
    seen = set()
    for m in models.values():
        for f in m._meta.get_fields():
            if (f.is_relation and f.many_to_one and not f.auto_created
                    and f.related_model.__name__ in models):
                key = (m.__name__, f.related_model.__name__, f.name)
                if key in seen:
                    continue
                seen.add(key)
                print(f'  {m.__name__} -> {f.related_model.__name__} '
                      f'[label="{f.name}"];')
    print('}')


if __name__ == '__main__':
    sys.exit(main())
