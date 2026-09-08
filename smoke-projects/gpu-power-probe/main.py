"""Read GPU power limits once and persist a sanitized result on shared storage."""
import json
from pathlib import Path
import subprocess

try:
    result = subprocess.run(
        ['nvidia-smi', '--query-gpu=name,power.limit,power.default_limit,power.max_limit',
         '--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=60,
    )
    rows = []
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            name, current, default, maximum = [part.strip() for part in line.split(',')]
            rows.append(dict(name=name, power_limit_w=float(current),
                             default_limit_w=float(default), max_limit_w=float(maximum)))
    payload = {'ok': bool(rows), 'gpus': rows}
except Exception:
    payload = {'ok': False, 'gpus': []}
text = json.dumps(payload)
Path(__file__).with_name('result.local.json').write_text(text + '\n')
print(text, flush=True)
raise SystemExit(0 if payload['ok'] else 1)
