# Isolated test project

Copy this directory to a disposable location, then run `git init -b main`, `git add .`, `git -c user.name=Test -c user.email=test@example.invalid commit -m initial`, and `python -m unittest`. This project has no network services or secrets. Any fake GitHub or Pi driver is a simulation and does not prove real GitHub or Pi behavior.

## Scheduler integration (Linux)

From the repository root run `python -m unittest discover -s python/tests -p test_test_project_integration.py -v`. It creates a disposable Git repository, clones it through the real scheduler, launches `fake_pi.py` as a JSONL subprocess, and runs the local unittest. GitHub responses are controlled in process; the Pi replacement uses only the loopback MCP bridge. No real GitHub or Pi behavior is claimed. Temporary workspaces and receipts are separate and removed after the test. A permitted skipped optional check is not a passed check. Windows skips this Linux process test.
