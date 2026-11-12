import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('board_fixture', root / 'board/server.py')
board = importlib.util.module_from_spec(spec)
spec.loader.exec_module(board)
labels = {1: [], 2: [], 3: ['agent'], 4: ['needs-human'], 5: [], 6: [], 7: [], 8: ['agent']}
issues = [{'number': n, 'title': title, 'state': 'open', 'labels': [{'name': x} for x in labels[n]],
           'html_url': f'https://github.com/fixture/repo/issues/{n}', 'author_association': 'OWNER'}
          for n, title in enumerate(['等待批准', '等待CI', '真实AI运行', '人工暂停', '只有PR未交付', '合并后收尾', 'PR关闭未合并', '返工排队'], 1)]
stages = {1: 'waiting_review', 2: 'waiting_checks', 6: 'finalizing', 7: 'closed', 8: 'superseded'}
host = {f'GH-{n}': {'stage': stage, 'reason': title, 'can_approve': n == 1, 'sha': 'a' * 40,
                   'delivery_id': f'delivery-{n}', 'pr': n + 100}
        for n, stage in stages.items() for title in [issues[n-1]['title']]}
state = {'running': [{'issue_identifier': 'GH-3', 'session_id': 'real-ai', 'started_at': '2026-11-11T12:00:00Z'}],
         'retrying': [], 'blocked': [], 'github_workflows': host}
pulls = {n: {'number': n + 100, 'url': f'https://github.com/fixture/repo/pull/{n + 100}', 'title': 'Fixture PR'} for n in labels}
with patch.object(board, 'load_tracker', return_value=('fixture/repo', 'fixture-token')), patch.object(board, 'github_issues', return_value=issues), patch.object(board, 'github_pulls', return_value=pulls), patch.object(board, 'symphony_state', return_value=state), patch.object(board, 'scan_sessions', return_value={}), patch.object(board, 'github_history', return_value=([], None)), patch.object(board, 'pause_reason', return_value='人工暂停'):
    data = board.build_cards()
    document = board.issues_page()
data.update({'runtime': board.agent_status(state), 'merge_waiting': ['GH-1','GH-2','GH-6'],
             'github_observed_after': 9999999999, 'state_observed_after': 9999999999, 'updated': 'isolated fixture'})
mock = '<script>window.testWrites=[];window.fetch=async (url,opts)=>{if(opts?.method==="POST")window.testWrites.push({url,body:JSON.parse(opts.body)});return {ok:true,status:200,json:async()=> opts?.method==="POST" ? {ok:true} : ' + json.dumps(data) + '};};</script>'
(root / 'work/board-handoff-fixture.html').write_text(document.replace('<script>', mock + '<script>', 1), encoding='utf-8')
