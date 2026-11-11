import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('board_fixture', root / 'board/server.py')
board = importlib.util.module_from_spec(spec)
spec.loader.exec_module(board)
cols = {key: '' for key, *_ in board.COLUMNS}
for number, lane in [(1, 'awaiting'), (2, 'running'), (3, 'blocked')]:
    cols[lane] += (f"<div class='bcard {lane}' data-issue='GH-TEST{number}' data-title='Isolated test' "
        f"data-state='{lane}' data-state-text='Original' data-agent='1' data-paused='{int(lane == 'blocked')}' "
        f"data-mg='{int(lane == 'awaiting')}' data-search='test'><div class='btitle'>Isolated test</div>"
        "<span class='bstat'>Original</span></div>")
data = {'cols': cols, 'counts': {'total': 3}, 'writable': True, 'updated': 'fixture', 'facts': {}, 'runtime': {}}
mock = '<script>window.fetch = async function(url, opts) { return {ok:true, status:200, json:async()=>'
mock += '(opts && opts.method === "POST" ? {ok:true} : ' + json.dumps(data) + ')}; };</script>'
with patch.object(board, 'load_tracker', return_value=('fixture/repo', '')):
    document = board.issues_page()
(root / 'work/board-state-fixture.html').write_text(document.replace('<script>', mock + '<script>', 1), encoding='utf-8')
