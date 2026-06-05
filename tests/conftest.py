"""
Shared fixtures and module loader for iib-tekton-utils tests.

multi-arch-builder.py uses a hyphenated filename so it cannot be imported
via the standard import mechanism. We load it with importlib at session
start and register it as "multi_arch_builder" in sys.modules so that all
test modules can simply ``import multi_arch_builder``.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent
TASK_DIR = REPO_ROOT / "task" / "iib-image-builder-oci-ta"
SCRIPT_PATH = TASK_DIR / "multi-arch-builder.py"
TASK_YAML_PATH = TASK_DIR / "iib-image-builder-oci-ta.yaml"


def _load_multi_arch_builder():
    """Load multi-arch-builder.py as the module 'multi_arch_builder'."""
    if "multi_arch_builder" in sys.modules:
        return sys.modules["multi_arch_builder"]
    spec = importlib.util.spec_from_file_location("multi_arch_builder", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["multi_arch_builder"] = module
    spec.loader.exec_module(module)
    return module


# Load at import time so ``import multi_arch_builder`` works in every test.
_load_multi_arch_builder()


# ---------------------------------------------------------------------------
# Common fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def build_config():
    """Return a minimal, valid BuildConfig instance."""
    import multi_arch_builder as mab

    return mab.BuildConfig(
        image_name="quay.io/org/index:v1.0",
        dockerfile_path="/tmp/Dockerfile",
        context_path="/tmp/context",
        platforms=["amd64", "arm64"],
        labels=["key=value"],
        cache_dir="/tmp/cache",
        commit_sha="abc123",
        opm_version="v1.40.0",
    )


@pytest.fixture()
def builder(build_config):
    """Return a MultiArchBuilder instance backed by the minimal BuildConfig."""
    import multi_arch_builder as mab

    return mab.MultiArchBuilder(build_config)


@pytest.fixture()
def sample_iib_metadata():
    """Sample IIB build metadata as used in PRs."""
    return {
        "opm_version": "opm-v1.48.0",
        "labels": {
            "com.redhat.index.delivery.version": "v4.19",
            "com.redhat.index.delivery.distribution_scope": "prod",
        },
        "binary_image": (
            "quay.io/operator-framework/upstream-registry-builder"
            "@sha256:7c8068817855b55e60ff5c2591c494130c2d105e0cc062836a5438a42935f8f8"
        ),
        "request_id": 89240,
        "arches": ["amd64"],
    }


@pytest.fixture()
def metadata_build_context(tmp_path, sample_iib_metadata, monkeypatch):
    """
    Build context with ``.iib-build-metadata.json`` and Tekton env vars set.
    """
    import json

    context = tmp_path / "index-build"
    context.mkdir()
    (context / ".iib-build-metadata.json").write_text(
        json.dumps(sample_iib_metadata), encoding="utf-8"
    )

    monkeypatch.setenv("IMAGE", "quay.io/org/index:v1.0")
    monkeypatch.setenv("COMMIT_SHA", "abc123def456")
    monkeypatch.setenv("CONTEXT", str(context))
    monkeypatch.setenv("DOCKERFILE", str(context / "index.Dockerfile"))
    monkeypatch.delenv("IIB_BUILD_METADATA_FILE_PATH", raising=False)

    return context
