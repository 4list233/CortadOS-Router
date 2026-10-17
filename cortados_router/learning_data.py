# PURPOSE: Own MCP collection publication, revision eligibility and learning interfaces.
# CONTRACT: RT-MCP-LEARNING-003; one current revision, no cross-partition leakage.
# DEPENDS: learning_source snapshots, task_evidence canonicalization, core text cleaning.
# CEILING: No live scheduling, teacher/model calls, weights, promotion or scorer changes.
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile

from .task_evidence import canonical, digest
from .learning_source import SourceError, read_observations

# Tokens in the complete student training example, including its answer. This is
# the unchanged training ceiling: smaller loses valid data, larger gets truncated.
MAX_TRAINING_TOKENS = 1536
TEACHER_EVIDENCE_NOTE = ('Outcome annotations are fallible historical evidence. Native completion is not correctness. '
                         'Answer using the existing twelve anchors; never convert runtime failures or planner verdicts into answers.')


def split_of(row_id: str) -> str:
    """Fixed by hash, forever: a row never moves between train and holdout, whatever gets added later."""
    return "holdout" if int(hashlib.sha1(row_id.encode()).hexdigest(), 16) % 100 < 15 else "train"


def read_rows(path):
    path = Path(path)
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def _fingerprint(text):
    from .core import clean_request
    return digest(clean_request(text) or text)


def outcome_annotation(observation):
    history = observation['feedback']
    latest = history[-1] if history else None
    return {**{key: observation[key] for key in ('native_outcome', 'worker', 'usage', 'elapsed_ms',
                                                'reason', 'planner_verdict', 'observation_digest')},
        'verification_status': 'planner_asserted' if latest else 'unreviewed',
        'summary': latest['summary'] if latest else None,
        'checks': latest['checks'] if latest else [],
        'feedback_ids': [row['feedback_id'] for row in history]}


def request_revision(observation):
    fingerprint = _fingerprint(observation['task'])
    group_id = observation['task_group_id']
    group = ('mcp-group:' + observation['client_id'] + ':' + group_id
             if group_id else 'mcp-task:' + fingerprint)
    return {'id': observation['learning_id'], 'source': 'mcp-worker', 'unit_kind': 'interactive',
        'request': observation['learning_text'], 'split': split_of(group), 'group_key': group,
        'task_fingerprint': fingerprint, 'scoring_digest': observation['scoring_digest'],
        'job_id': observation['job_id'], 'observation_digest': observation['observation_digest'],
        'outcome_annotation': outcome_annotation(observation)}


def eligible_rows(requests, observations):
    current = {row['job_id']: row for row in observations if row.get('schema') == 'cortados.mcp-learning.v1'}
    parents, linked, partitions, reasons = {}, {}, {}, {}

    def find(key):
        parents.setdefault(key, key)
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    for row in requests:
        rid = row['id']
        mcp = row.get('source') == 'mcp-worker'
        key = row['group_key'] if mcp else rid
        split = row['split']
        partitions[rid] = ('holdout' if split == 'holdout' else
            'valid' if int(hashlib.sha1(('v' + key).encode()).hexdigest(), 16) % 10 == 0 else 'train')
        # Build components across *all* historical revisions, including those
        # now superseded: an old partition conflict must not disappear on review.
        links = [('fingerprint', row['task_fingerprint'] if mcp else _fingerprint(row['request']))]
        if mcp:
            links.append(('group', row['group_key']))
            active = current.get(row['job_id'])
            if active is None or active['learning_id'] != rid:
                reasons[rid] = 'superseded'
            elif not active['eligibility']:
                reasons[rid] = active['exclusion_reason']
        find(rid)
        for link in links:
            if link in linked:
                parents[find(rid)] = find(linked[link])
            else:
                linked[link] = rid
    component_partitions = {}
    for rid, partition in partitions.items():
        component_partitions.setdefault(find(rid), set()).add(partition)
    for rid in partitions:
        if len(component_partitions[find(rid)]) > 1:
            reasons[rid] = 'split_conflict'
    rows = sorted((row for row in requests if row['id'] not in reasons), key=lambda row: row['id'])
    return {'rows': rows, 'partitions': {row['id']: partitions[row['id']] for row in rows},
            'excluded': [{'id': rid, 'reason': reasons[rid]} for rid in sorted(reasons)]}


def bind_snapshot(data_dir, requests=None):
    """Bind a complete projection once; caller retains returned objects for its phase."""
    data = Path(data_dir)
    return eligible_rows(read_rows(data / 'requests.jsonl') if requests is None else requests,
                         read_rows(data / 'mcp-observations.jsonl'))


def _diagnostic(code, source=None, identifier=None):
    return {'component': 'mcp_learning', 'code': code,
            'source': str(source) if source is not None else None, 'id': identifier}


def _replace(path, content):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.' + path.name + '.', delete=False) as stream:
            temporary = stream.name
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def collect_mcp(state_dir, data_dir):
    state, data = Path(state_dir), Path(data_dir)
    diagnostics, added = [], 0
    outcome = {'status': 'ok', 'added': 0, 'observations': 0, 'diagnostics': diagnostics}
    if not state.is_absolute() or not data.is_absolute() or not data.is_dir():
        outcome.update(status='degraded')
        diagnostics.append(_diagnostic('mcp_source_invalid'))
        return outcome
    try:
        with (data / 'mcp-collection.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                diagnostics.append(_diagnostic('mcp_collection_busy'))
                outcome['status'] = 'degraded'
                return outcome
            observations = read_observations(state)
            path = data / 'requests.jsonl'
            prefix = path.read_bytes() if path.exists() else b''
            requests = [json.loads(line) for line in prefix.splitlines() if line.strip()]
            have = {row['id'] for row in requests}
            new = [request_revision(row) for row in observations if row.get('job_id') and row['eligibility']
                   and row['learning_id'] not in have]
            combined = requests + new
            selected = eligible_rows(combined, observations)
            for row in observations:
                if row.get('job_id') and row['exclusion_reason']:
                    diagnostics.append(_diagnostic(row['exclusion_reason'], state / 'router/router.db', row['learning_id']))
            for exclusion in selected['excluded']:
                if exclusion['reason'] in ('split_conflict', 'superseded'):
                    diagnostics.append(_diagnostic(exclusion['reason'], None, exclusion['id']))
            suffix = ''.join(canonical(row) + '\n' for row in new).encode('utf-8')
            if suffix and prefix and not prefix.endswith(b'\n'):
                suffix = b'\n' + suffix
            if new or not path.exists():
                _replace(path, prefix + suffix)
            added = len(new)
            outcome['added'] = added
            with path.open('rb') as requests_file:
                os.fsync(requests_file.fileno())
            projection = ''.join(canonical(row) + '\n' for row in observations).encode('utf-8')
            _replace(data / 'mcp-observations.jsonl', projection)
            outcome['observations'] = len(observations)
    except SourceError as exc:
        diagnostics.append(_diagnostic(exc.code, exc.source))
        outcome['status'] = 'degraded'
    except (OSError, ValueError, KeyError, TypeError) as exc:
        diagnostics.append(_diagnostic('mcp_source_invalid'))
        outcome.update(status='degraded', added=added)
    return outcome


def teacher_payload(batch):
    return [dict({'i': index, 'unit_kind': row['unit_kind'], 'request': row['request']},
                 **({'outcome_annotation': row['outcome_annotation']} if row.get('source') == 'mcp-worker' else {}))
            for index, row in enumerate(batch)]


def label_provenance(row):
    if row.get('source') != 'mcp-worker':
        return {}
    return {'observation_digest': row['observation_digest'],
            'feedback_ids': row['outcome_annotation']['feedback_ids']}


def training_tokenizer(base=None):
    from transformers import AutoTokenizer
    base = base or os.environ.get('CORTADOS_S1_BASE')
    if not base:
        raise ValueError('Selected training base unavailable')
    return AutoTokenizer.from_pretrained(base, local_files_only=True, trust_remote_code=False)


def training_context(row, messages, tokenizer):
    if row.get('source') != 'mcp-worker':
        return None
    if tokenizer is None:
        return {'component': 'mcp_learning', 'code': 'training_tokenizer_unavailable', 'id': row['id']}
    try:
        count = len(tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=False))
    except Exception:
        return {'component': 'mcp_learning', 'code': 'training_tokenizer_unavailable', 'id': row['id']}
    if count > MAX_TRAINING_TOKENS:
        return {'component': 'mcp_learning', 'code': 'training_context_too_large', 'id': row['id'], 'tokens': count}
    return None


def _content_hash(path):
    checksum = hashlib.sha256()
    with path.open('rb') as stream:
        # Bytes per hash read, not a data cap. 1 MiB keeps large adapter hashes
        # bounded in memory; smaller costs syscalls, larger increases peak RSS.
        while chunk := stream.read(1024 * 1024):
            checksum.update(chunk)
    return checksum.hexdigest()


def _file_hashes(path):
    if not path:
        return None
    path = Path(path)
    if path.is_file():
        return {path.name: _content_hash(path)}
    if path.is_dir():
        return {str(file.relative_to(path)): _content_hash(file)
                for file in sorted(path.rglob('*')) if file.is_file()}
    return {'unavailable': str(path)}


def evaluation_identity(rows, *, fail_up, outward_at, adapter=None, base=None, partitions=None,
                        scorer_path=None, labels=None, scorer_config=None, scorer_material=None):
    base_path = base
    if base and not Path(base).exists():
        try:
            from huggingface_hub import snapshot_download
            base_path = snapshot_download(repo_id=str(base), local_files_only=True)
        except Exception:
            base_path = base
    material = {'rows': [{'id': row['id'], 'request_digest': digest(row['request']),
                          'unit_kind': row['unit_kind'], 'group_key': row.get('group_key'),
                          'split': row['split'], 'teacher': teacher} for row, teacher in rows],
                'partitions': partitions, 'labels': labels, 'fail_up': fail_up, 'outward_at': outward_at,
                'adapter': _file_hashes(adapter), 'base': _file_hashes(base_path),
                'scorer_config': scorer_config, 'scorer_material': scorer_material,
                'scorer': _file_hashes(scorer_path), 'base_identity': str(base),
                'core_source': _file_hashes(Path(__file__).with_name('core.py'))}
    return digest(canonical(material))
