#!/usr/bin/env python3
"""Fail a ranking release gate on mismatched evidence or any per-case regression."""
import argparse
import json
from pathlib import Path

ARMS = ('lexical/unscoped', 'lexical/scoped', 'hybrid/unscoped', 'hybrid/scoped')


def compare(baseline, candidate):
    errors = []
    for field in ('fixture_sha256', 'runner_sha256', 'method'):
        if not baseline.get(field) or baseline.get(field) != candidate.get(field):
            errors.append('Missing or mismatched ' + field)
    for report in (baseline, candidate):
        if not report.get('runtime_sha256'):
            errors.append('Missing runtime hashes')
    left, right = baseline.get('sources', {}), candidate.get('sources', {})
    if not left or set(left) != set(right):
        errors.append('Source set differs or is empty')
    for name, src in left.items():
        other = right.get(name, {})
        if not src.get('sha256_ok') or not other.get('sha256_ok'):
            errors.append('Unverified source ' + name)
        for field in ('frozen_bytes', 'blocks'):
            if src.get(field) != other.get(field):
                errors.append('Source differs: ' + name + '/' + field)
    # The runner snapshots into random temporary directories. A directory-based
    # repository hash can therefore differ while denoting the same source set.
    # Require identical scope membership and exact identities for named remotes.
    for name, src in left.items():
        old = src.get('repo_id'); new = right.get(name, {}).get('repo_id')
        if not isinstance(old, str) or not isinstance(new, str):
            errors.append('Missing repository scope: ' + name)
            continue
        if old != new and not (old.startswith('directory:') and new.startswith('directory:')):
            errors.append('Repository identity differs: ' + name)
        old_members = {k for k, v in left.items() if v.get('repo_id') == old}
        new_members = {k for k, v in right.items() if v.get('repo_id') == new}
        if old_members != new_members:
            errors.append('Repository scope membership differs: ' + name)
    for field in ('model_fingerprint', 'dimension', 'format'):
        a = baseline.get('semantic_build', {}).get(field)
        if a is None or a != candidate.get('semantic_build', {}).get(field):
            errors.append('Missing or mismatched semantic ' + field)
    a = {c['id']: c for c in baseline.get('cases', [])}
    b = {c['id']: c for c in candidate.get('cases', [])}
    if (not a or set(a) != set(b) or len(a) != len(baseline.get('cases', []))
            or len(b) != len(candidate.get('cases', []))):
        errors.append('Case set differs, duplicates or is empty')
    for arm in ARMS:
        if arm not in baseline.get('arms', {}) or arm not in candidate.get('arms', {}):
            errors.append('Missing arm ' + arm)
    positives = 0
    for key, case in a.items():
        other = b.get(key, {})
        if case.get('answer_present') != other.get('answer_present'):
            errors.append('Answerability differs: ' + key)
        if not case.get('answer_present'):
            continue
        positives += 1
        for arm in ARMS:
            old = case.get('results', {}).get(arm, {})
            new = other.get('results', {}).get(arm, {})
            if not old.get('supported') or not new.get('supported'):
                errors.append('Unsupported or missing result: ' + key + '/' + arm)
            for metric in ('exact', 'broad'):
                if type(old.get(metric)) is not bool or type(new.get(metric)) is not bool:
                    errors.append('Unscored positive: ' + key + '/' + arm + '/' + metric)
                elif old[metric] and not new[metric]:
                    errors.append('Regression: ' + key + '/' + arm + '/' + metric)
    if not positives:
        errors.append('No scored positives')
    return {'passed': not errors, 'positive_cases': positives, 'errors': errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    args = parser.parse_args()
    result = compare(json.loads(args.baseline.read_text()), json.loads(args.candidate.read_text()))
    print(json.dumps(result, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
