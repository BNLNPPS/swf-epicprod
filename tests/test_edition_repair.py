"""The rebinding repair's decisions (pythia6-lhapdf 1.1.0, 2026-10-27).

Pure: classify / plan_problems over plan dictionaries, no database.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from pcs.edition_repair import (EMPTY, MOVES, SPLIT, UNCHANGED,  # noqa: E402
                                classify, plan_problems)

OLD = 'p4491.pythia6-6-lhapdf'
NEW = 'p4491.pythia6-lhapdf-1.1.0'


def entry(label='pc6778', outcome=MOVES, n=2, **over):
    e = {'pc': label, 'key': OLD, 'lifecycle': 'active', 'requestors': [],
         'metadata': {}, 'outcome': outcome,
         'target': {'key': NEW, 'label': '', 'lifecycle': ''} if outcome == MOVES else None,
         'editions': [{'pk': i} for i in range(n)]}
    e.update(over)
    return e


def test_classify_whole_move():
    assert classify(OLD, [NEW, NEW]) == MOVES


def test_classify_unchanged():
    assert classify(OLD, [OLD, OLD]) == UNCHANGED


def test_classify_split_between_keys():
    assert classify(OLD, [NEW, 'p4491.other']) == SPLIT


def test_classify_partial_move_is_split():
    assert classify(OLD, [OLD, NEW]) == SPLIT


def test_classify_no_editions():
    assert classify(OLD, []) == EMPTY


def test_three_whole_moves_apply():
    plan = [entry('pc6778'), entry('pc6779'), entry('pc6781')]
    assert plan_problems(plan, expect_editions=6) == []


def test_edition_count_must_match():
    plan = [entry('pc6778'), entry('pc6779'), entry('pc6781', n=3)]
    assert plan_problems(plan, expect_editions=6) == ['7 editions bound, 6 expected']


def test_every_outcome_but_moves_refused():
    for outcome in (UNCHANGED, SPLIT, EMPTY):
        problems = plan_problems([entry(outcome=outcome)])
        assert problems == [f'pc6778: {outcome}, not one whole move']


def test_inactive_configuration_refused():
    assert plan_problems([entry(lifecycle='superseded')]) == [
        'pc6778: lifecycle superseded, not active']


def test_associations_refused():
    assert len(plan_problems([entry(requestors=['SIDIS'])])) == 1
    assert len(plan_problems([entry(metadata={'requestors': {}})])) == 1


def test_existing_inactive_target_refused():
    e = entry(target={'key': NEW, 'label': 'pc7001', 'lifecycle': 'retired'})
    assert plan_problems([e]) == ['pc6778: target pc7001 is retired, not active']


def test_target_among_the_superseded_refused():
    e = entry(target={'key': NEW, 'label': 'pc6779', 'lifecycle': 'active'})
    assert plan_problems([e, entry('pc6779')]) == [
        'pc6778: target pc6779 is itself being superseded']


def test_existing_active_target_accepted():
    e = entry(target={'key': NEW, 'label': 'pc7001', 'lifecycle': 'active'})
    assert plan_problems([e]) == []
