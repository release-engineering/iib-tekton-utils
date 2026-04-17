# iib-tekton-utils

Tekton tasks and supporting tooling for building multi-architecture Operator Index Images using [IIB](https://github.com/release-engineering/iib) and [buildah](https://buildah.io/).

## Repository layout

```
.
├── Containerfile.iib-build-task          # Container image used by the Tekton task
├── task/
│   └── iib-image-builder-oci-ta/
│       ├── iib-image-builder-oci-ta.yaml # Tekton Task definition
│       ├── multi-arch-builder.py         # Python orchestration script (runs inside the task)
│       └── README.md                     # Task-specific usage docs
└── tests/
    ├── conftest.py                        # Shared fixtures and module loader
    ├── unit/
    │   └── test_multi_arch_builder.py    # Unit tests for multi-arch-builder.py
    └── tekton/
        └── test_iib_image_builder_task.py # Structural tests for the Tekton task YAML
```

## Task overview

`iib-image-builder-oci-ta` builds source code into multi-architecture Operator Index Images. It:

1. Extracts the source from a Trusted Artifact (`use-trusted-artifact` step)
2. Generates an OPM cache from the file-based catalog
3. Builds container images for each target architecture (amd64, arm64, ppc64le, s390x) via `buildah bud`
4. Creates and pushes a multi-architecture manifest list

See [`task/iib-image-builder-oci-ta/README.md`](task/iib-image-builder-oci-ta/README.md) for parameter reference and usage examples.

---

## Running the tests

### Prerequisites

- Python 3.9 or later
- `pip`

### Install dependencies

```bash
pip install pytest pytest-mock pyyaml tenacity
```

Or, if your `pip` supports the `pyproject.toml` extras syntax:

```bash
pip install ".[test]"
```

### Run all tests

```bash
pytest
```

`pytest` discovers `tests/` automatically because `pyproject.toml` sets `testpaths = ["tests"]`.

### Run with verbose output

```bash
pytest -v
```

### Run only unit tests

```bash
pytest tests/unit/
```

### Run only Tekton task tests

```bash
pytest tests/tekton/
```

### Run a single test class or test

```bash
# All tests in a class
pytest tests/unit/test_multi_arch_builder.py::TestRunCmd

# A single test
pytest tests/unit/test_multi_arch_builder.py::TestRunCmd::test_buildah_network_403_raises_external_service_error
```

### Stop on first failure

```bash
pytest -x
```

### Show local variables in tracebacks

```bash
pytest -l
```

---

## Test structure

### `tests/conftest.py`

`multi-arch-builder.py` uses a hyphenated filename that cannot be imported with the standard `import` statement. `conftest.py` loads it via `importlib.util` at session start and registers it in `sys.modules` as `multi_arch_builder`, so every test module can simply:

```python
import multi_arch_builder as mab
```

It also provides two session-scoped fixtures used across unit tests:

| Fixture | Description |
|---|---|
| `build_config` | A minimal, valid `BuildConfig` instance |
| `builder` | A `MultiArchBuilder` instance backed by `build_config` |

### `tests/unit/test_multi_arch_builder.py`

Unit tests for every public function and class in `multi-arch-builder.py`. External calls (`subprocess.run`, filesystem side effects) are mocked with `unittest.mock.patch` so no container runtime or OPM binary is required.

| Test class | What is covered |
|---|---|
| `TestRegexReverseSearch` | Match ordering (bottom-up iteration), empty stderr, no-match |
| `TestRunCmd` | Successful execution, default params, strict vs non-strict mode, buildah manifest-rm "image not known", HTTP 403/50x and closed-pipe → `ExternalServiceError`, opm error regex → `IIBError` |
| `TestGenerateCacheLocally` | OPM command arguments, pre-existing cache cleanup, empty cache → `IIBError`, non-existent cache dir → `IIBError`, `OPM_VERSION` env var |
| `TestBuildConfig` | Default `arch_map`, custom `arch_map` |
| `TestValidateDockerfile` | Existing Dockerfile → `True`, missing → `False` |
| `TestUpdateCATrust` | Skips when bundle absent, copies and updates when present, re-raises `IIBError` |
| `TestVerifyImageArchitecture` | Matching arch, mismatched arch → `ExternalServiceError`, missing key skips check, custom `arch_map`, `containers-storage:` scheme in inspect command |
| `TestBuildImage` | buildah flags (`--override-arch`, `--arch`, `--tls-verify`, etc.), label injection, context path appended last, `ExternalServiceError` on network failure |
| `TestCreateAndPushManifestList` | Tag count with and without `commit_sha`, tolerates "Manifest list not found locally", unexpected rm error re-raises, push uses `--all` and `docker://` scheme |
| `TestLoadConfigFromEnv` | Defaults for all env vars, relative vs absolute CONTEXT and DOCKERFILE, comma-split PLATFORMS |
| `TestMain` | Missing IMAGE exits 1, missing COMMIT_SHA exits 1, prints JSON to stdout, writes JSON to file, exits 1 on `IIBError` |

### `tests/tekton/test_iib_image_builder_task.py`

Structural validation of `iib-image-builder-oci-ta.yaml` using `pyyaml`. No Kubernetes or Tekton cluster is needed.

| Test class | What is covered |
|---|---|
| `TestTaskStructure` | `apiVersion`, `kind`, `name`, description, annotations, labels |
| `TestTaskParameters` | Required params have no default, optional params have correct defaults, all params have `type` and `description`, PLATFORMS default covers all four architectures |
| `TestTaskResults` | `IMAGE_DIGEST`, `IMAGE_REF`, `IMAGE_URL` present and described |
| `TestTaskVolumes` | All five volumes present, `trusted-ca` uses ConfigMap with `optional: true`, others are `emptyDir` |
| `TestStepTemplate` | Memory limit, CPU request, required env vars (`IMAGE`, `COMMIT_SHA`, `PLATFORMS`, `OPM_VERSION`, `CACHE_DIR`), `shared` and `workdir` volume mounts |
| `TestTaskSteps` | Both steps present, `use-trusted-artifact` references `SOURCE_ARTIFACT`, `build-multi-arch` script invokes the Python builder, extracts all three results, mounts `varlibcontainers` and `trusted-ca`, runs as root with `SETFCAP`, passes `--ca-bundle`, uses `set -euo pipefail` |

---

## Building the container image

The `Containerfile.iib-build-task` builds the image used inside the Tekton task:

```bash
buildah build -f Containerfile.iib-build-task -t iib-build-task:latest .
```

The image is based on `quay.io/konflux-ci/buildah-task` and bundles:
- Multiple OPM versions (v1.26.4, v1.40.0, v1.44.0, v1.48.0)
- `skopeo`, `jq`, `python3`, and required Python packages (`tenacity`, `GitPython`, `kubernetes`, `ruamel.yaml`)
- `multi-arch-builder.py` installed at `/usr/local/bin/`

## Contributing

1. Fork the repository and create a feature branch.
2. Make your changes.
3. Run the full test suite (`pytest -v`) and ensure all tests pass.
4. Open a pull request against `main`.
