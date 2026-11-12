"""One-shot local reload; refuse to interrupt running or queued development."""
import json
import subprocess
import time
import urllib.request

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
url = 'http://127.0.0.1:8320/api/v1/state'

def state():
    with opener.open(url, timeout=5) as response:
        return json.load(response)

def idle(snapshot):
    return not snapshot.get('running') and all(
        entry.get('delay_type') == 'merge_poll' for entry in snapshot.get('retrying', []))

deadline = time.monotonic() + 300
next_report = 0
while time.monotonic() < deadline:
    snapshot = state()
    if idle(snapshot):
        time.sleep(2)
        if idle(state()):
            break
    if time.monotonic() >= next_report:
        print('Waiting for active work to finish:', [e.get('issue_identifier') for e in snapshot.get('running', [])], flush=True)
        next_report = time.monotonic() + 30
    time.sleep(3)
else:
    raise SystemExit('Not reloaded: active work did not finish within five minutes.')

command = r'''
$scheduler = Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python' -and $_.CommandLine -match '-m symphony' -and $_.CommandLine -match 'symphony-local' } | Select-Object -First 1
if (-not $scheduler) {
    throw 'Scheduler process not found; refusing reload'
}
$state = Invoke-RestMethod -Uri 'http://127.0.0.1:8320/api/v1/state' -TimeoutSec 5
if (@($state.running).Count -gt 0) { throw 'Worker started; refusing reload' }
Stop-Process -Id $scheduler.ProcessId -ErrorAction Stop
Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine='cmd /c D:\workspace\playground\symphony-local\start-scheduler.bat'
} | Select-Object ProcessId,ReturnValue | Format-List
'''
subprocess.run(['powershell', '-NoProfile', '-Command', command], check=True)
for _ in range(30):
    try:
        current = state()
        print('Scheduler API ready after idle reload; running:', len(current.get('running', [])), flush=True)
        break
    except OSError:
        time.sleep(2)
else:
    raise SystemExit('Reload started but API readiness was not confirmed.')
