"""Keep latest project's SDK first; append preinstalled external evaluator extras."""
import importlib.metadata
import runpy
import site
import sys

site.addsitedir('/Users/a/.pyenv/versions/3.12.8/lib/python3.12/site-packages')
site.addsitedir('/Users/a/.cache/uv/archive-v0/RnfjjWJ_J3H-uhQ9')
assert importlib.metadata.version('databricks-sdk') == '0.143.0'
assert importlib.metadata.version('playwright') == '1.60.0'
assert importlib.metadata.version('pyte') == '0.8.2'
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
