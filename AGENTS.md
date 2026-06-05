# iib-tekton-utils

Tekton task for building multi-architecture Operator Index Images (IIB) using Python orchestration with buildah.

## What This Does

Builds container images for OLM file-based catalogs across multiple architectures (amd64, arm64, ppc64le, s390x), generates OPM cache, and pushes multi-arch manifest lists.

## Tech Stack

- Python 3.12+ (orchestration)
- Tekton Tasks (Kubernetes CI/CD)
- Buildah (container builds)
- OPM (Operator Package Manager)
- pytest (testing)

## Key Files

- `task/iib-image-builder-oci-ta/iib-image-builder-oci-ta.yaml` - Tekton Task definition
- `task/iib-image-builder-oci-ta/multi-arch-builder.py` - Python build orchestrator
- `Containerfile.iib-build-task` - Container image for the build task
- `pyproject.toml` - Project config and test dependencies
- `tox.ini` - Test and lint orchestration (pytest, ruff, yamllint)

## Directory Structure

```
task/iib-image-builder-oci-ta/   # Tekton task + Python script
tests/
  conftest.py                    # Shared fixtures, module loader for hyphenated filename
  unit/test_multi_arch_builder.py    # Unit tests for Python script
  tekton/test_iib_image_builder_task.py  # YAML structure validation
.github/workflows/               # CI workflows
```

## Commands

```bash
# Install test dependencies
pip install ".[test]"

# Run all checks (tests, ruff, yamllint) — same as CI
pip install tox && tox

# Run individual tox environments
tox -e unit          # unit tests
tox -e tekton        # Tekton YAML validation tests
tox -e ruff          # Python lint
tox -e ruff-format   # Python format check
tox -e yamllint      # YAML lint

# Run all tests (without tox)
pytest

# Run unit tests only
pytest tests/unit/

# Run Tekton task validation tests only
pytest tests/tekton/

# Single-file lint (fast feedback)
ruff check task/iib-image-builder-oci-ta/multi-arch-builder.py

# Single-file type check
mypy task/iib-image-builder-oci-ta/multi-arch-builder.py

# Build container image
buildah build -f Containerfile.iib-build-task -t iib-build-task:latest .

# Setup pre-commit hooks
pip install pre-commit && pre-commit install
```

## Pattern References

- **Adding a new Tekton parameter**: Follow pattern in `task/iib-image-builder-oci-ta/iib-image-builder-oci-ta.yaml:17-58`
- **Adding retry logic**: See `_build_image()` at multi-arch-builder.py:427 for tenacity decorator pattern
- **Adding a new test class**: Follow `TestRunCmd` pattern in tests/unit/test_multi_arch_builder.py:66

## Architecture Notes

- `MultiArchBuilder` class in multi-arch-builder.py:353 orchestrates the build
- `generate_cache_locally()` at :295 runs OPM to create FBC cache
- Retry logic via tenacity for buildah operations (:427, :524)
- Exception hierarchy: `IIBBaseException` > `IIBError`, `ExternalServiceError`
- Tests use `conftest.py` to load hyphenated `multi-arch-builder.py` via importlib

## Test Coverage

- Unit tests mock all subprocess/filesystem calls (no container runtime needed)
- Tekton tests validate YAML structure with pyyaml (no cluster needed)
- Key fixtures: `build_config`, `builder` (conftest.py:46, :62)
