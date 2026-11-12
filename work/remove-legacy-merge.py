from pathlib import Path
path = Path('python/symphony/github/workflow.py')
text = path.read_text(encoding='utf-8')
start = text.index('def _admit_merge(')
end = text.index('def waiting(', start)
text = text[:start] + text[end:]
text = text.replace('''        # A published delivery belongs to the host: evaluate the merge gate
        # inline every admission instead of dispatching an agent that would
        # just poll checks and burn tokens. Repairs still get a worker.
''', '')
path.write_text(text, encoding='utf-8')
path = Path('python/tests/test_github_rework.py')
text = path.read_text(encoding='utf-8').replace('workflow, "_admit_merge",', 'workflow, "delivery_state",')
path.write_text(text, encoding='utf-8')
