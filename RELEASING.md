# Releasing schedviz

Releases are cut and uploaded **manually with twine** from a local machine.
(There is no CI/CD publish step — CI only runs the test suite.)

## One-time setup

```bash
pip install --upgrade build twine
```

Put a PyPI API token in `~/.pypirc` (or export `TWINE_USERNAME=__token__` and
`TWINE_PASSWORD=pypi-...` when uploading):

```ini
[pypi]
  username = __token__
  password = pypi-AgE...your-token...
```

## Cut a release

1. Bump the version in `pyproject.toml` (`[project] version`) and, if you keep
   one, `schedviz/__init__.py` `__version__`. Commit it.

2. Build a clean sdist + wheel:

   ```bash
   rm -rf dist build *.egg-info
   python -m build
   twine check dist/*
   ```

3. (Optional but recommended) smoke-test the built wheel in a throwaway venv:

   ```bash
   python -m venv /tmp/sv && /tmp/sv/bin/pip install dist/schedviz-*.whl
   /tmp/sv/bin/schedviz --demo --collisions
   ```

4. Upload to TestPyPI first if you want to verify the page, then PyPI:

   ```bash
   twine upload --repository testpypi dist/*   # optional dry run
   twine upload dist/*
   ```

5. Tag and push:

   ```bash
   git tag -a vX.Y.Z -m "schedviz X.Y.Z"
   git push origin main --tags
   ```

6. Create a GitHub release from the tag (notes only — no artifacts needed):

   ```bash
   gh release create vX.Y.Z --title "schedviz X.Y.Z" --notes "..."
   ```
