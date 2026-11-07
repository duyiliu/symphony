"""End-to-end local scheduler exercise; GitHub and Pi are controlled fakes."""
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from collections import Counter
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from symphony import mcp_bridge, workflow_store
from symphony.github import client
from symphony.orchestrator import Orchestrator
from symphony.workflow import WorkflowError

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'tests' / 'fixtures' / 'test-project'

class FakeGitHub:
    def __init__(self, sha, checks):
        self.sha, self.checks = sha, checks
        self.merged = False
        self.closed = False
        self.labels = {'agent'}
        self.calls = Counter()
    def __call__(self, method, path, params, body, settings):
        self.calls[method + ' ' + path] += 1
        issue = {'number':1, 'title':'Local test', 'state':'closed' if self.closed else 'open', 'labels':[{'name':name} for name in sorted(self.labels)]}
        pr = {'state':'open', 'draft':False, 'merged':self.merged, 'head':{'sha':self.sha,'repo':{'full_name':'local/test'}}, 'base':{'ref':'main','repo':{'full_name':'local/test'}}, 'user':{'login':'agent'}}
        root = '/repos/local/test'
        if method == 'GET' and path == root + '/issues': data = [] if params.get('state') == 'closed' or self.closed else [issue]
        elif method == 'GET' and path == root + '/issues/1': data = issue
        elif method == 'GET' and path == root + '/issues/1/events': data = [{'id':1,'event':'labeled','label':{'name':'agent'}}]
        elif method == 'POST' and path == '/graphql':
            if 'closedByPullRequestsReferences' in body['query']:
                data = {'data':{'repository':{'issue':{'closedByPullRequestsReferences':{'nodes':[{'number':2,'state':'OPEN'}], 'pageInfo':{'hasNextPage':False}}}}}}
            else: data = {'data':{'repository':{'pullRequest':{'headRefOid':self.sha,'baseRefName':'main','mergeStateStatus':'CLEAN','reviewDecision':None}}}}
        elif method == 'GET' and path == root + '/pulls/2': data = pr
        elif method == 'GET' and path.endswith('/check-runs'): data = {'check_runs':self.checks}
        elif method == 'GET' and (path.endswith('/statuses') or path.endswith('/files')): data = []
        elif method == 'POST' and path.endswith('/labels'):
            self.labels.update(body['labels']); data = {}
        elif method == 'POST' and path.endswith('/comments'): data = {}
        elif method == 'PUT' and path.endswith('/merge'):
            if body.get('sha') != self.sha: raise AssertionError('incorrect merge SHA')
            self.merged = True; data = {'merged':True}
        elif method == 'PATCH' and path == root + '/issues/1': self.closed = True; data = {}
        elif method == 'DELETE' and '/labels/' in path:
            self.labels.discard(urllib.parse.unquote(path.rsplit('/', 1)[1])); data = {}
        else: raise AssertionError('Unexpected request: ' + method + ' ' + path)
        return {'status':200,'body':data}

@unittest.skipUnless(sys.platform.startswith('linux'), 'requires Linux process groups and bash')
class TestProjectIntegration(unittest.TestCase):
    def test_scheduler_workflow(self):
        summaries = []
        summary_path = os.environ.get('SYMPHONY_INTEGRATION_SUMMARY')
        original_connect = socket.socket.connect
        def loopback_only(sock, address):
            if not isinstance(address, tuple) or address[0] != '127.0.0.1':
                raise AssertionError('outbound socket blocked')
            return original_connect(sock, address)
        with tempfile.TemporaryDirectory(prefix='symphony-integration-') as tmp, patch.dict(os.environ, {'PATH':os.environ['PATH'], 'HOME':tmp, 'LANG':'C.UTF-8'}, clear=True), patch.object(socket.socket, 'connect', loopback_only):
            source = Path(tmp) / 'source'; source.mkdir()
            for name in ('app.py','test_app.py'): shutil.copy(FIXTURE / name, source / name)
            def git(*args): return subprocess.check_output(['git','-C',str(source),*args], text=True, stderr=subprocess.STDOUT).strip()
            git('init','-b','main'); git('add','.'); git('-c','user.name=Test','-c','user.email=test@example.invalid','commit','-m','fixture')
            sha = git('rev-parse','HEAD')
            for scenario, conclusion, checksha, expected in [('missing',None,sha,'waiting_checks'),('success_skipped','success',sha,'merged'),('failure','failure',sha,'needs_repair'),('wrong_sha','success','0'*40,'waiting_checks')]:
                checks = [] if conclusion is None else [{'name':'tests','status':'completed','conclusion':conclusion,'head_sha':checksha}]
                if scenario == 'success_skipped': checks.append({'name':'optional','status':'completed','conclusion':'skipped','head_sha':sha})
                fake = FakeGitHub(sha, checks)
                base = Path(tmp) / scenario; base.mkdir()
                config = {'tracker':{'kind':'github','provider':{'repo':'local/test','token':'fake-token'},'required_labels':[],'active_states':['open'],'terminal_states':['closed'], 'github_workflow':{'enabled':True,'state_dir':str(base/'receipts'),'base_branch':'main','checks':{'required':['tests'],'allow_skipped':['optional']}}}, 'workspace':{'root':str(base/'workspaces')}, 'hooks':{'after_create':'git clone ' + shlex.quote(str(source)) + ' .'}, 'polling':{'interval_ms':600000}, 'agent':{'max_turns':1,'max_concurrent_agents':1}, 'runtime':{'kind':'pi','command':shlex.quote(sys.executable)+' '+shlex.quote(str(FIXTURE/'fake_pi.py')),'turn_timeout_ms':15000,'mcp':{'enabled':True}}}
                wf = base/'WORKFLOW.md'; wf.write_text('---\n'+json.dumps(config)+'\n---\nLocal issue {{ issue.identifier }}\n')
                event = threading.Event(); done = []
                old_done = Orchestrator._on_worker_done
                def on_done(obj, issue_id, ref, reason):
                    done.append(str(reason)); result = old_done(obj, issue_id, ref, reason); event.set(); return result
                with patch.object(client,'request',fake), patch.object(client,'perform_request',fake), patch.object(Orchestrator,'_on_worker_done',on_done):
                    store = workflow_store.start_store(str(wf)); mcp_bridge.start(); orch = Orchestrator(store).start()
                    try:
                        self.assertTrue(event.wait(25), 'scheduler did not finish')
                    finally:
                        orch.stop(); mcp_bridge.stop(); workflow_store.stop_store()
                output = base/'workspaces'/'GH-1'/'integration-result.json'
                self.assertTrue(output.exists(), f'no fake Pi result: {done}')
                result = json.loads(output.read_text()); receipt = json.loads((base/'receipts'/'1.json').read_text())
                self.assertEqual(result['git_sha'],sha); self.assertEqual(result['test_exit'],0); self.assertIn('Ran 1 test', result['test_output']); self.assertEqual(result['merge']['status'],expected)
                with self.assertRaises(ProcessLookupError): os.kill(result['pid'], 0)
                self.assertIsNone(mcp_bridge.endpoint())
                for entry in orch.retry_attempts.values():
                    if entry.get('timer'): entry['timer'].join(timeout=2)
                self.assertFalse(any(entry.get('timer') and entry['timer'].is_alive() for entry in orch.retry_attempts.values()))
                merge_count = fake.calls['PUT /repos/local/test/pulls/2/merge']
                self.assertEqual(merge_count, 1 if expected == 'merged' else 0)
                self.assertEqual(bool(receipt.get('merged')), expected == 'merged')
                if expected != 'merged':
                    self.assertEqual(result['pause']['status'], 'paused')
                    self.assertTrue(receipt['blocked'])
                    self.assertIn('integration_decision', receipt['error'])
                    self.assertEqual(fake.labels, {'needs-human'})
                summaries.append({'scenario':scenario,'checks':checks,'decision':expected,'git_commit':sha,'fake_pi_pid':result['pid'],'local_test_exit':result['test_exit'],'receipt':receipt,'requests':dict(fake.calls),'merge_requests':merge_count,'scheduler_done':done,'paused':result['pause'],'labels':sorted(fake.labels),'pi_process_exited':True, 'bridge_stopped':True, 'retry_timers_stopped':True})
            config['tracker']['github_workflow']['checks']['allow_skipped'] = ['tests']
            wf.write_text('---\n'+json.dumps(config)+'\n---\nPrompt\n')
            with self.assertRaises(WorkflowError) as error: workflow_store.WorkflowStore(str(wf))
            self.assertEqual(error.exception.reason, 'invalid_github_workflow_checks')
            summaries.append({'scenario':'overlap','decision':'rejected','reason':error.exception.reason})
        for item in summaries: item['temporary_files_cleaned'] = not os.path.exists(tmp)
        if summary_path:
            Path(summary_path).write_text(json.dumps(summaries,indent=2))
        print('Integration summary: '+json.dumps(summaries))
