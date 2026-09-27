"""Ensures `pytest` run from the backend/ directory can `import app.*`.

Placing conftest.py here makes pytest insert this directory at the front of
sys.path, so tests use the real package layout (app.config, app.backtest, ...).
"""
