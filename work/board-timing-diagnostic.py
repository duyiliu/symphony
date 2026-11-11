import importlib.util
import os
import time

os.environ['SYMPHONY_WORKFLOW'] = 'D:/workspace/playground/symphony-local/config/WORKFLOW.local.md'
os.environ['SYMPHONY_ENV'] = 'D:/workspace/playground/symphony-local/.env'
os.environ['SYMPHONY_SESSIONS'] = 'D:/workspace/playground/symphony-local/sessions'
os.environ['SYMPHONY_SESSIONS_LIVE'] = 'D:/workspace/playground/symphony-local/sessions-live'
spec = importlib.util.spec_from_file_location('board_diag', 'D:/workspace/git/symphony/board/server.py')
board = importlib.util.module_from_spec(spec)
spec.loader.exec_module(board)
for name in ('github_issues', 'github_pulls', 'github_history', 'scan_sessions', 'symphony_state', 'pause_reason'):
    original = getattr(board, name)
    def wrapped(*args, _fn=original, _name=name, **kwargs):
        start = time.monotonic()
        try:
            return _fn(*args, **kwargs)
        finally:
            print(f'{_name} {time.monotonic()-start:.3f}s', flush=True)
    setattr(board, name, wrapped)
start = time.monotonic()
board.issues_page()
print(f'issues_page total {time.monotonic()-start:.3f}s', flush=True)
