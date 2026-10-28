"""The EVGEN-input manifest stops at the dataset's event target
(JEDI 41354, pc6783-6786, 2026-10-27).

Pure: the Rucio file list is stubbed, no database.
"""
import os
import sys
import types
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from pcs.commands import _evgen_manifest_from_inputs  # noqa: E402

DID = 'epic:/EVGEN/SIDIS/sample'


def _task(files, target=None, source='included'):
    return SimpleNamespace(
        inputs=[{'did': DID}],
        dataset=SimpleNamespace(expected_events=target,
                                expected_events_source=source)), files


def _manifest(monkeypatch, files, events_per_job=100, **kw):
    task, files = _task(files, **kw)
    stub = types.ModuleType('pcs.services')
    stub.fetch_jlab_rucio_did_files = lambda scope, name: list(files)
    monkeypatch.setitem(sys.modules, 'pcs.services', stub)
    return _evgen_manifest_from_inputs(task, events_per_job)


def _files(n, events=10000):
    # Reverse order: Rucio's listing order is not the manifest's.
    return [{'name': f'/EVGEN/SIDIS/sample/f{i:03d}.hepmc3.tree.root',
             'events': events} for i in reversed(range(n))]


def _events(rows):
    return sum(int(r.split(',')[2]) for r in rows)


def test_rows_stop_at_the_target(monkeypatch):
    # 50 files x 10k events = 500k input; the target is 50k.
    rows = _manifest(monkeypatch, _files(50), target=50000)
    assert len(rows) == 500
    assert _events(rows) == 50000


def test_ten_times_the_target_in_input(monkeypatch):
    # The reported case: 5M events of input, 500k target, 100 per row.
    rows = _manifest(monkeypatch, _files(500), target=500000)
    assert len(rows) == 5000
    assert _events(rows) == 500000


def test_cut_is_a_whole_row_reaching_the_target(monkeypatch):
    # 9999-event files chunk to 100 rows of 99; 1000 events need 11 rows.
    rows = _manifest(monkeypatch, _files(3, events=9999), target=1000)
    assert len(rows) == 11
    assert {r.split(',')[2] for r in rows} == {'99'}
    assert _events(rows) == 1089


def test_rows_follow_file_name_order(monkeypatch):
    rows = _manifest(monkeypatch, _files(5), target=10000)
    assert {r.split(',')[0] for r in rows} == {'SIDIS/sample/f000'}
    assert [r.split(',')[3] for r in rows] == [f'{i:04d}' for i in range(100)]


def test_rows_follow_did_name_order(monkeypatch):
    # Two matched DIDs listed in either order select the same rows.
    by_did = {'/EVGEN/SIDIS/a': [{'name': '/EVGEN/SIDIS/a/f.hepmc3.tree.root', 'events': 10000}],
              '/EVGEN/SIDIS/b': [{'name': '/EVGEN/SIDIS/b/f.hepmc3.tree.root', 'events': 10000}]}
    stub = types.ModuleType('pcs.services')
    stub.fetch_jlab_rucio_did_files = lambda scope, name: list(by_did['/' + name.lstrip('/')])
    monkeypatch.setitem(sys.modules, 'pcs.services', stub)
    ds = SimpleNamespace(expected_events=10000, expected_events_source='included')
    picks = []
    for dids in (['epic:/EVGEN/SIDIS/b', 'epic:/EVGEN/SIDIS/a'],
                 ['epic:/EVGEN/SIDIS/a', 'epic:/EVGEN/SIDIS/b']):
        task = SimpleNamespace(inputs=[{'did': d} for d in dids], dataset=ds)
        picks.append(_evgen_manifest_from_inputs(task, 100))
    assert picks[0] == picks[1]
    assert {r.split(',')[0] for r in picks[0]} == {'SIDIS/a/f'}


def test_input_short_of_the_target_keeps_every_row(monkeypatch):
    rows = _manifest(monkeypatch, _files(2), target=1000000)
    assert len(rows) == 200


def test_no_included_target_keeps_every_row(monkeypatch):
    assert len(_manifest(monkeypatch, _files(2))) == 200
    assert len(_manifest(monkeypatch, _files(2), target=100, source='estimated')) == 200
