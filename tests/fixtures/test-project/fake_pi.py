"""Minimal Pi JSONL substitute; runs only inside an isolated test workspace."""
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request

for line in sys.stdin:
    message = json.loads(line)
    if message.get('type') != 'prompt':
        continue
    git_sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    run = subprocess.run([sys.executable, '-m', 'unittest', '-v'], capture_output=True, text=True)
    with open('.pi/mcp.json', encoding='utf-8') as config_file:
        config = json.load(config_file)['mcpServers']['symphony']
    url = config['url']
    if urllib.parse.urlparse(url).hostname != '127.0.0.1':
        raise RuntimeError('non-loopback MCP rejected')
    def call(arguments):
        data = json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/call', 'params':{'name':'github_workflow','arguments':arguments}}).encode()
        req = urllib.request.Request(url, data=data, headers={**config['headers'], 'Content-Type':'application/json'})
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.load(response)['result']
        if result.get('isError'):
            raise RuntimeError(result['content'][0]['text'])
        return json.loads(result['content'][0]['text'])
    if run.returncode:
        raise RuntimeError('local tests failed')
    delivery = call({'action':'deliver','issue':'1','pr':2,'tests':'python -m unittest -v: 1 passed'})
    result = call({'action':'merge','issue':'1'})
    paused = None
    if result['status'] != 'merged':
        paused = call({'action':'pause','issue':'1',
                       'reason':{'integration_decision':result['status']}})
    with open('integration-result.json', 'w', encoding='utf-8') as out:
        json.dump({'pid':os.getpid(),'git_sha':git_sha,'test_exit':run.returncode,'test_output':run.stderr,'delivery':delivery,'merge':result,'pause':paused}, out)
    print(json.dumps({'type':'response','id':message['id'],'success':True}), flush=True)
    print(json.dumps({'type':'agent_settled'}), flush=True)
