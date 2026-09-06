"""Makes the repo root importable so tests can `from hooks import ...`.

The modules under test (`hooks`, `transcript_events`, ...) live at the repo
root, but pytest only puts the test file's own directory on `sys.path`. A
conftest.py here adds the root, so `pytest tests` works the same as
`python -m pytest`.
"""
