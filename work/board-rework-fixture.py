from pathlib import Path

source = Path(__file__).with_name('board-state-fixture.py')
code = source.read_text(encoding='utf-8').replace('work/board-state-fixture.html', 'work/board-rework-fixture.html')
code = code.replace('data-mg=', "data-pr='https://github.com/fixture/repo/pull/2' data-pr-num='2' data-mg=")
exec(compile(code, str(source), 'exec'), {'__file__': str(source)})
