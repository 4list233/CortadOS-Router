# Inert terminal source: never invokes a provider or touches credential/settings stores.
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import termios
import time
import tty

log, mode = Path(sys.argv[1]), sys.argv[2]
tty.setraw(0)
with log.open('a') as stream:
    stream.write(json.dumps({'pid': os.getpid(), 'pgid': os.getpgrp()}) + '\n')
if mode == 'normal':
    helper = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(60)'],
                              start_new_session=True)
    with log.open('a') as stream:
        stream.write(json.dumps({'pid': helper.pid, 'pgid': helper.pid, 'helper': True}) + '\n')
print('Trust this workspace' if mode == 'trust' else 'READY', flush=True)
# A ten-second inert input window bounds the fixture even if production fails its own deadline.
end = time.monotonic() + 10
received = b''
while time.monotonic() < end:
    if select.select([0], [], [], .1)[0]:
        data = os.read(0, 4096)
        if not data:
            break
        received += data
        with log.open('a') as stream:
            stream.write(json.dumps({'input_hex': data.hex()}) + '\n')
        if mode == 'trust':
            break
        if b'\r' in received or b'\n' in received:
            print('QUOTA_DONE', flush=True)
            time.sleep(.5)
            break
