"""Synthetic contract checks; never open the private 50-question evaluation."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

RUNNER = Path(__file__).with_name('evaluate_frozen.py')


def fixture(tmp_path, *, human=False):
    sources, cases = {}, []
    for agent in ('claude', 'codex'):
        path = tmp_path / f'{agent}.jsonl'
        if agent == 'claude':
            rows = [
                {'type': 'user', 'uuid': 'ask', 'sessionId': agent, 'cwd': str(tmp_path),
                 'message': {'role': 'user', 'content': 'What is the amber label?'}},
                {'type': 'assistant', 'uuid': 'answer', 'sessionId': agent, 'cwd': str(tmp_path),
                 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'The amber label is accepted.'}]}},
            ]
        else:
            rows = [
                {'type': 'session_meta', 'payload': {'id': agent, 'cwd': str(tmp_path)}},
                {'type': 'response_item', 'payload': {'id': 'codex-answer', 'type': 'message', 'role': 'assistant',
                 'content': [{'type': 'output_text', 'text': 'The amber label is accepted.'}]}},
            ]
        raw = ''.join(json.dumps(r) + '\n' for r in rows).encode()
        path.write_bytes(raw)
        sources[agent] = {'agent': agent, 'session_id': agent, 'transcript': str(path),
                          'frozen_bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        cases.append({'id': agent, 'agent': agent, 'session_id': agent, 'transcript': str(path),
                      'question': 'amber', 'anchor': 'amber', 'source_line': 2, 'block_ordinal': 0,
                      'answer_present': True, 'review_status': 'pending',
                      **({'message_id': 'answer'} if agent == 'claude' else {'message_key': 'codex-answer'})})
    packet = {'format': 'recall-human-review-candidates-v1' if human else 'synthetic-contract-test',
              'sources': sources, 'cases': cases}
    return packet


def write_fixture(tmp_path, packet):
    path = tmp_path / 'questions.json'
    path.write_text(json.dumps(packet))
    path.with_suffix('.sha256').write_text(hashlib.sha256(path.read_bytes()).hexdigest())
    return path


def run(tmp_path, packet, *flags):
    path = write_fixture(tmp_path, packet)
    env = dict(os.environ, HOME=str(tmp_path), RECALL_DB=str(tmp_path / 'untouched.db'),
               RECALL_SETTINGS=str(tmp_path / 'untouched-settings.json'), RECALL_LOG_FILE=str(tmp_path / 'untouched.log'))
    for key in ('CLAUDE_ENV_FILE', 'CLAUDE_CODE_SESSION_ID', 'RECALL_SESSION_ID', 'RECALL_PROJECT_HASH'):
        env.pop(key, None)
    out = tmp_path / 'result.json'
    result = subprocess.run([sys.executable, str(RUNNER), '--fixture', str(path), '--out', str(out), *flags],
                            env=env, capture_output=True, text=True, timeout=20)
    assert not (tmp_path / 'untouched.db').exists()
    return result, json.loads(out.read_text()) if out.exists() else None


def test_codex_exact_labels_and_unsupported_legacy(tmp_path):
    result, report = run(tmp_path, fixture(tmp_path))
    assert result.returncode == 0, result.stderr
    assert report['arms']['lexical/scoped']['exact_hit_at_5'] == 1
    assert report['arms']['legacy/scoped']['positives'] == 1
    assert report['arms']['legacy/scoped']['unsupported'] == 1
    assert report['arms']['legacy/scoped']['exact_hit_at_5'] is None
    codex = next(c for c in report['cases'] if c['id'] == 'codex')
    assert codex['results']['lexical/scoped']['exact'] is True
    assert codex['results']['legacy/scoped']['supported'] is False


def test_pending_human_packet_cannot_run_retrieval(tmp_path):
    packet = fixture(tmp_path, human=True)
    result, report = run(tmp_path, packet)
    assert result.returncode != 0 and 'Human review is pending' in result.stderr
    assert report is None


def test_validation_allows_pending_review_without_retrieval(tmp_path):
    result, report = run(tmp_path, fixture(tmp_path, human=True), '--validate-only')
    assert result.returncode == 0, result.stderr
    assert report['exact_labels'] == 2 and report['human_pending'] == 2
    assert report['retrieval_runs'] == 0


def test_status_alone_cannot_invent_human_provenance(tmp_path):
    packet = fixture(tmp_path, human=True)
    for case in packet['cases']:
        case['review_status'] = 'accepted'
    result, _ = run(tmp_path, packet)
    assert result.returncode != 0 and 'Human review is pending' in result.stderr


def test_source_hash_mismatch_stops_before_results(tmp_path):
    packet = fixture(tmp_path)
    Path(packet['sources']['codex']['transcript']).write_text('{}\n')
    result, report = run(tmp_path, packet, '--validate-only')
    assert result.returncode != 0 and 'hash/length mismatch' in result.stderr
    assert report is None


def test_label_must_resolve_to_the_specified_source_line(tmp_path):
    packet = fixture(tmp_path)
    packet['cases'][1]['message_key'] = 'nonexistent'
    result, report = run(tmp_path, packet, '--validate-only')
    assert result.returncode != 0 and 'source line and message key disagree' in result.stderr
    assert report is None


def test_codex_byte_offset_labels(tmp_path):
    packet = fixture(tmp_path)
    path = Path(packet['sources']['codex']['transcript'])
    rows = path.read_bytes().splitlines(keepends=True)
    record = json.loads(rows[1]); del record['payload']['id']
    raw = rows[0] + (json.dumps(record) + '\n').encode(); path.write_bytes(raw)
    packet['sources']['codex'].update(frozen_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    packet['cases'][1].update(message_key=str(len(rows[0])), start_byte=len(rows[0]))
    result, report = run(tmp_path, packet)
    assert result.returncode == 0, result.stderr
    assert report['arms']['lexical/scoped']['exact_hit_at_5'] == 1


def test_wrong_source_byte_offset_is_rejected(tmp_path):
    packet = fixture(tmp_path)
    packet['cases'][1]['start_byte'] = 0
    result, _ = run(tmp_path, packet, '--validate-only')
    assert result.returncode != 0 and 'source byte offset disagrees' in result.stderr


def test_exact_label_does_not_require_an_anchor(tmp_path):
    packet = fixture(tmp_path)
    for case in packet['cases']:
        case['anchor'] = None
    result, report = run(tmp_path, packet)
    assert result.returncode == 0, result.stderr
    assert report['arms']['lexical/scoped']['exact_hit_at_5'] == 1
    assert report['arms']['lexical/scoped']['broad_hit_at_5'] is None


def test_fixture_hash_mismatch_is_fatal(tmp_path):
    path = write_fixture(tmp_path, fixture(tmp_path))
    path.write_text(path.read_text() + ' ')
    result = subprocess.run([sys.executable, str(RUNNER), '--fixture', str(path), '--validate-only'],
                            text=True, capture_output=True, timeout=10)
    assert result.returncode != 0 and 'fixture hash mismatch' in result.stderr
