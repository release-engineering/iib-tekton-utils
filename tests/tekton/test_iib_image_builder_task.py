"""
Tests for the iib-image-builder-oci-ta Tekton task definition.

These tests validate the structure, parameters, results, volumes, and step
configuration of the YAML without requiring a live Kubernetes/Tekton cluster.
"""

from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Load the task YAML once for the entire module
# ---------------------------------------------------------------------------

TASK_YAML_PATH = (
    Path(__file__).parent.parent.parent
    / "task"
    / "iib-image-builder-oci-ta"
    / "iib-image-builder-oci-ta.yaml"
)


@pytest.fixture(scope="module")
def task():
    """Parse and return the Tekton task YAML as a dict."""
    assert TASK_YAML_PATH.exists(), f"Task YAML not found at {TASK_YAML_PATH}"
    with TASK_YAML_PATH.open() as fh:
        return yaml.safe_load(fh)


@pytest.fixture(scope="module")
def spec(task):
    return task["spec"]


@pytest.fixture(scope="module")
def params_by_name(spec):
    return {p["name"]: p for p in spec["params"]}


@pytest.fixture(scope="module")
def results_by_name(spec):
    return {r["name"]: r for r in spec["results"]}


@pytest.fixture(scope="module")
def volumes_by_name(spec):
    return {v["name"]: v for v in spec["volumes"]}


@pytest.fixture(scope="module")
def steps_by_name(spec):
    return {s["name"]: s for s in spec["steps"]}


# ---------------------------------------------------------------------------
# Top-level resource structure
# ---------------------------------------------------------------------------


class TestTaskStructure:
    def test_api_version(self, task):
        assert task["apiVersion"] == "tekton.dev/v1"

    def test_kind(self, task):
        assert task["kind"] == "Task"

    def test_name(self, task):
        assert task["metadata"]["name"] == "iib-image-builder-oci-ta"

    def test_has_description(self, spec):
        assert spec.get("description"), "Task must have a description"

    def test_has_pipeline_min_version_annotation(self, task):
        annotations = task["metadata"].get("annotations", {})
        assert "tekton.dev/pipelines.minVersion" in annotations

    def test_app_version_label(self, task):
        labels = task["metadata"].get("labels", {})
        assert "app.kubernetes.io/version" in labels


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------


REQUIRED_PARAMS = {"IMAGE", "SOURCE_ARTIFACT"}
OPTIONAL_PARAMS_WITH_DEFAULTS = {
    "COMMIT_SHA": "",
    "CONTEXT": ".",
    "DOCKERFILE": "./Dockerfile",
    "IIB_BUILD_METADATA_FILE_PATH": ".iib-build-metadata.json",
    "STORAGE_DRIVER": "overlay",
    "caTrustConfigMapKey": "ca-bundle.crt",
    "caTrustConfigMapName": "trusted-ca",
}


class TestTaskParameters:
    def test_all_required_params_present(self, params_by_name):
        for name in REQUIRED_PARAMS:
            assert name in params_by_name, f"Required param '{name}' is missing"

    def test_required_params_have_no_default(self, params_by_name):
        for name in REQUIRED_PARAMS:
            assert "default" not in params_by_name[name], (
                f"Required param '{name}' should not have a default value"
            )

    def test_optional_params_have_correct_defaults(self, params_by_name):
        for name, default in OPTIONAL_PARAMS_WITH_DEFAULTS.items():
            assert name in params_by_name, f"Optional param '{name}' is missing"
            assert str(params_by_name[name].get("default", "")) == str(default), (
                f"Param '{name}' has wrong default"
            )

    def test_all_params_have_type(self, params_by_name):
        for name, param in params_by_name.items():
            assert "type" in param, f"Param '{name}' is missing 'type'"

    def test_all_params_have_description(self, params_by_name):
        for name, param in params_by_name.items():
            assert param.get("description"), f"Param '{name}' is missing 'description'"

    def test_iib_build_metadata_file_path_default(self, params_by_name):
        assert (
            params_by_name["IIB_BUILD_METADATA_FILE_PATH"]["default"] == ".iib-build-metadata.json"
        )

    def test_image_param_is_string_type(self, params_by_name):
        assert params_by_name["IMAGE"]["type"] == "string"

    def test_source_artifact_param_is_string_type(self, params_by_name):
        assert params_by_name["SOURCE_ARTIFACT"]["type"] == "string"


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


EXPECTED_RESULTS = {"IMAGE_DIGEST", "IMAGE_REF", "IMAGE_URL"}


class TestTaskResults:
    def test_all_expected_results_present(self, results_by_name):
        for name in EXPECTED_RESULTS:
            assert name in results_by_name, f"Expected result '{name}' is missing"

    def test_all_results_have_description(self, results_by_name):
        for name, result in results_by_name.items():
            assert result.get("description"), f"Result '{name}' is missing 'description'"

    def test_image_digest_description_mentions_digest(self, results_by_name):
        desc = results_by_name["IMAGE_DIGEST"]["description"].lower()
        assert "digest" in desc

    def test_image_ref_description_mentions_reference(self, results_by_name):
        desc = results_by_name["IMAGE_REF"]["description"].lower()
        assert "image" in desc

    def test_image_url_description_mentions_url_or_image(self, results_by_name):
        desc = results_by_name["IMAGE_URL"]["description"].lower()
        assert "image" in desc or "url" in desc


# ---------------------------------------------------------------------------
# Volumes
# ---------------------------------------------------------------------------


EXPECTED_VOLUMES = {"shared", "trusted-ca", "varlibcontainers", "workdir", "cache"}


class TestTaskVolumes:
    def test_all_expected_volumes_present(self, volumes_by_name):
        for name in EXPECTED_VOLUMES:
            assert name in volumes_by_name, f"Expected volume '{name}' is missing"

    def test_trusted_ca_volume_references_configmap(self, volumes_by_name):
        vol = volumes_by_name["trusted-ca"]
        assert "configMap" in vol, "trusted-ca volume must be backed by a ConfigMap"

    def test_trusted_ca_configmap_name_uses_param(self, volumes_by_name):
        configmap_name = volumes_by_name["trusted-ca"]["configMap"]["name"]
        assert "caTrustConfigMapName" in configmap_name

    def test_trusted_ca_configmap_is_optional(self, volumes_by_name):
        configmap = volumes_by_name["trusted-ca"]["configMap"]
        assert configmap.get("optional") is True

    def test_cache_and_workdir_are_empty_dirs(self, volumes_by_name):
        for name in ("cache", "workdir", "shared", "varlibcontainers"):
            vol = volumes_by_name[name]
            assert "emptyDir" in vol, f"Volume '{name}' should be an emptyDir"


# ---------------------------------------------------------------------------
# stepTemplate
# ---------------------------------------------------------------------------


class TestStepTemplate:
    def test_step_template_sets_memory_limit(self, spec):
        step_template = spec.get("stepTemplate", {})
        limits = step_template.get("computeResources", {}).get("limits", {})
        assert "memory" in limits, "stepTemplate must define a memory limit"

    def test_step_template_sets_cpu_request(self, spec):
        step_template = spec.get("stepTemplate", {})
        requests = step_template.get("computeResources", {}).get("requests", {})
        assert "cpu" in requests, "stepTemplate must define a cpu request"

    def test_step_template_propagates_required_env_vars(self, spec):
        step_template = spec.get("stepTemplate", {})
        env_names = {e["name"] for e in step_template.get("env", [])}
        for required in ("IMAGE", "COMMIT_SHA", "IIB_BUILD_METADATA_FILE_PATH", "CACHE_DIR"):
            assert required in env_names, f"stepTemplate must expose env var '{required}'"

    def test_step_template_mounts_shared_and_workdir_volumes(self, spec):
        step_template = spec.get("stepTemplate", {})
        mount_names = {m["name"] for m in step_template.get("volumeMounts", [])}
        assert "shared" in mount_names
        assert "workdir" in mount_names


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


class TestTaskSteps:
    def test_use_trusted_artifact_step_present(self, steps_by_name):
        assert "use-trusted-artifact" in steps_by_name

    def test_build_multi_arch_step_present(self, steps_by_name):
        assert "build-multi-arch" in steps_by_name

    def test_use_trusted_artifact_passes_source_artifact_param(self, steps_by_name):
        step = steps_by_name["use-trusted-artifact"]
        args = step.get("args", [])
        combined = " ".join(str(a) for a in args)
        assert "SOURCE_ARTIFACT" in combined

    def test_build_step_script_invokes_python_builder(self, steps_by_name):
        script = steps_by_name["build-multi-arch"].get("script", "")
        assert "multi-arch-builder.py" in script

    def test_build_step_script_extracts_image_digest(self, steps_by_name):
        script = steps_by_name["build-multi-arch"].get("script", "")
        assert "IMAGE_DIGEST" in script

    def test_build_step_script_extracts_image_url(self, steps_by_name):
        script = steps_by_name["build-multi-arch"].get("script", "")
        assert "IMAGE_URL" in script

    def test_build_step_script_creates_image_ref(self, steps_by_name):
        script = steps_by_name["build-multi-arch"].get("script", "")
        assert "IMAGE_REF" in script

    def test_build_step_mounts_varlibcontainers(self, steps_by_name):
        step = steps_by_name["build-multi-arch"]
        mount_names = {m["name"] for m in step.get("volumeMounts", [])}
        assert "varlibcontainers" in mount_names

    def test_build_step_mounts_trusted_ca(self, steps_by_name):
        step = steps_by_name["build-multi-arch"]
        mount_names = {m["name"] for m in step.get("volumeMounts", [])}
        assert "trusted-ca" in mount_names

    def test_build_step_runs_as_root(self, steps_by_name):
        security_context = steps_by_name["build-multi-arch"].get("securityContext", {})
        assert security_context.get("runAsUser") == 0

    def test_build_step_has_setfcap_capability(self, steps_by_name):
        security_context = steps_by_name["build-multi-arch"].get("securityContext", {})
        added_caps = security_context.get("capabilities", {}).get("add", [])
        assert "SETFCAP" in added_caps

    def test_build_step_passes_ca_bundle_flag_to_script(self, steps_by_name):
        script = steps_by_name["build-multi-arch"].get("script", "")
        assert "--ca-bundle" in script

    def test_build_step_script_uses_set_euo_pipefail(self, steps_by_name):
        script = steps_by_name["build-multi-arch"].get("script", "")
        assert "set -euo pipefail" in script

    def test_build_step_passes_metadata_file_path_to_python(self, steps_by_name):
        script = steps_by_name["build-multi-arch"].get("script", "")
        assert "--iib-build-metadata-file-path" in script
        assert "IIB_BUILD_METADATA_FILE_PATH" in script

    def test_build_step_sets_metadata_file_path_env(self, steps_by_name):
        step = steps_by_name["build-multi-arch"]
        env_names = {e["name"] for e in step.get("env", [])}
        assert "IIB_BUILD_METADATA_FILE_PATH" in env_names

    def test_trusted_artifact_step_has_image(self, steps_by_name):
        assert steps_by_name["use-trusted-artifact"].get("image"), (
            "use-trusted-artifact step must specify an image"
        )

    def test_build_step_has_image(self, steps_by_name):
        assert steps_by_name["build-multi-arch"].get("image"), (
            "build-multi-arch step must specify an image"
        )
