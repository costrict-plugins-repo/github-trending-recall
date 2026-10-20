import copy
from check_retrieval_gate import ARMS, compare


def report():
    return dict(fixture_sha256='fixture', runner_sha256='runner', method='top-5',
        runtime_sha256={'scripts/memory_store.py': 'hash'},
        sources={'one': dict(sha256_ok=True, frozen_bytes=30, repo_id='repo', blocks=2)},
        semantic_build=dict(model_fingerprint='model', dimension=384, format='f32le-v1'),
        arms={a: {} for a in ARMS}, cases=[dict(id='one', answer_present=True,
            results={a: dict(supported=True, exact=True, broad=True) for a in ARMS})])


def test_same_evidence_passes_with_different_runtime():
    a = report(); b = copy.deepcopy(a); b['runtime_sha256']['scripts/memory_store.py'] = 'new'
    assert compare(a, b)['passed']


def test_regression_is_not_hidden_by_new_hits():
    a = report(); b = copy.deepcopy(a)
    b['cases'][0]['results']['hybrid/scoped']['exact'] = False
    assert not compare(a, b)['passed']


def test_missing_or_changed_contract_fails():
    for field in ('fixture_sha256', 'runner_sha256', 'sources', 'semantic_build', 'cases', 'runtime_sha256'):
        a = report(); b = copy.deepcopy(a); del b[field]
        assert not compare(a, b)['passed'], field
    a = report(); b = copy.deepcopy(a); b['semantic_build']['model_fingerprint'] = 'different'
    assert not compare(a, b)['passed']
    b = copy.deepcopy(a); del b['cases'][0]['results']['lexical/scoped']
    assert not compare(a, b)['passed']


def test_random_snapshot_directory_identity_preserves_scope_partition():
    a = report(); b = copy.deepcopy(a)
    a['sources']['one']['repo_id'] = 'directory:old'
    b['sources']['one']['repo_id'] = 'directory:new'
    assert compare(a, b)['passed']
    a['sources']['two'] = dict(a['sources']['one'], repo_id='directory:separate')
    b['sources']['two'] = dict(b['sources']['one'])
    assert not compare(a, b)['passed']
