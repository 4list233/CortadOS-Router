# Inert native-source child: protocol adversary only, never reads a credential or provider.
import json
import os
from pathlib import Path
import sys
import time

control = json.loads(Path(os.environ['QUOTA_EXAM_CONTROL']).read_text())
request = json.load(sys.stdin)
with Path(control['log']).open('a') as stream:
    stream.write(json.dumps({'request': request, 'cwd': os.getcwd(),
                             'marker': os.environ.get('QUOTA_EXAM_MARKER')}) + '\n')
time.sleep(control.get('delay', 0))
if control.get('mode') == 'failure':
    sys.exit(3)
start = time.time()
record = {'source': 'cursor native getCurrentPeriodUsage', 'windows': [
    {'window': 'native month', 'pool': pool, 'used_pct': control.get('used', 20),
     'window_s': 2678400, 'reset_epoch': start + 86400}
    for pool in ('info', 'cursor-models', 'other-models')]}
response = {'schema': 'cortados.quota-source.response.v1', 'nonce': request['nonce'],
            'profile': request['profile'], 'source': record['source'], 'record': record, 'error': None,
            'proof': {'valid_until_epoch': control.get('valid_until_epoch', start + 3600)}}
mode = control.get('mode')
if mode == 'nonce': response['nonce'] = '0' * 64
if mode == 'profile': response['profile'] = 'unreviewed-profile'
if mode == 'source': response['source'] = 'unknown-reader'
if mode == 'extra': response['observed_at'] = start
if mode == 'malformed': response['record'] = None
if mode == 'proof-missing': response.pop('proof')
if mode == 'proof-null': response['proof'] = {'valid_until_epoch': None}
if mode == 'proof-nan': response['proof'] = {'valid_until_epoch': float('nan')}
if mode == 'proof-extra': response['proof']['token'] = 'SYNTHETIC_PRIVATE_TOKEN'
if mode == 'replay':
    previous = Path(control['log']).with_suffix('.response')
    if previous.exists(): response = json.loads(previous.read_text())
    else: previous.write_text(json.dumps(response))
print(json.dumps(response), flush=True)
