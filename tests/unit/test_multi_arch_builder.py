"""
Unit tests for task/iib-image-builder-oci-ta/multi-arch-builder.py.

The module is loaded via tests/conftest.py using importlib (because the
filename contains a hyphen) and registered in sys.modules as
``multi_arch_builder``.
"""

import json
import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

import multi_arch_builder as mab

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_completed_process(returncode=0, stdout="", stderr=""):
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


# ---------------------------------------------------------------------------
# _regex_reverse_search
# ---------------------------------------------------------------------------


class TestRegexReverseSearch:
    def test_returns_match_from_last_line(self):
        proc = _make_completed_process(stderr="first line\nError: something bad\nlast info")
        match = mab._regex_reverse_search(r"^Error: (.+)$", proc)
        assert match is not None
        assert match.group(1) == "something bad"

    def test_returns_first_match_found_in_reverse_order(self):
        """Iteration is bottom-up so the *last* occurrence is matched first."""
        proc = _make_completed_process(stderr="Error: first\nError: last")
        match = mab._regex_reverse_search(r"^Error: (.+)$", proc)
        assert match is not None
        assert match.group(1) == "last"

    def test_returns_none_when_no_match(self):
        proc = _make_completed_process(stderr="nothing interesting here")
        result = mab._regex_reverse_search(r"^Error: (.+)$", proc)
        assert result is None

    def test_returns_none_for_empty_stderr(self):
        proc = _make_completed_process(stderr="")
        result = mab._regex_reverse_search(r"^Error: (.+)$", proc)
        assert result is None


# ---------------------------------------------------------------------------
# run_cmd
# ---------------------------------------------------------------------------


class TestRunCmd:
    @patch("subprocess.run")
    def test_returns_stdout_on_success(self, mock_run):
        mock_run.return_value = _make_completed_process(stdout="hello\n")
        result = mab.run_cmd(["echo", "hello"])
        assert result == "hello\n"

    @patch("subprocess.run")
    def test_sets_default_params(self, mock_run):
        mock_run.return_value = _make_completed_process(stdout="")
        mab.run_cmd(["true"])
        _, kwargs = mock_run.call_args
        assert kwargs.get("universal_newlines") is True
        assert kwargs.get("encoding") == "utf-8"
        assert kwargs.get("stderr") == subprocess.PIPE
        assert kwargs.get("stdout") == subprocess.PIPE

    @patch("subprocess.run")
    def test_raises_iib_error_on_nonzero_returncode(self, mock_run):
        mock_run.return_value = _make_completed_process(returncode=1, stderr="failure output")
        with pytest.raises(mab.IIBError):
            mab.run_cmd(["false"])

    @patch("subprocess.run")
    def test_custom_exc_msg_propagated(self, mock_run):
        mock_run.return_value = _make_completed_process(returncode=1, stderr="")
        with pytest.raises(mab.IIBError, match="custom message"):
            mab.run_cmd(["false"], exc_msg="custom message")

    @patch("subprocess.run")
    def test_non_strict_does_not_raise_on_failure(self, mock_run):
        mock_run.return_value = _make_completed_process(returncode=1, stderr="err", stdout="out")
        result = mab.run_cmd(["false"], strict=False)
        assert result == "out"

    @patch("subprocess.run")
    def test_buildah_manifest_rm_image_not_known_raises_iib_error(self, mock_run):
        mock_run.return_value = _make_completed_process(
            returncode=1, stderr="Error: image not known"
        )
        with pytest.raises(mab.IIBError, match="Manifest list not found locally"):
            mab.run_cmd(["buildah", "manifest", "rm", "quay.io/org/img:latest"])

    @patch("subprocess.run")
    def test_buildah_network_403_raises_external_service_error(self, mock_run):
        mock_run.return_value = _make_completed_process(
            returncode=1,
            stderr="error creating build container: 403 Forbidden",
        )
        with pytest.raises(mab.ExternalServiceError):
            mab.run_cmd(["buildah", "bud", "-t", "myimage:latest", "."])

    @patch("subprocess.run")
    def test_buildah_network_503_raises_external_service_error(self, mock_run):
        mock_run.return_value = _make_completed_process(
            returncode=1,
            stderr="error creating build container: 503 Service Unavailable",
        )
        with pytest.raises(mab.ExternalServiceError):
            mab.run_cmd(["buildah", "bud", "-t", "myimage:latest", "."])

    @patch("subprocess.run")
    def test_buildah_closed_pipe_raises_external_service_error(self, mock_run):
        mock_run.return_value = _make_completed_process(
            returncode=1,
            stderr="read/write on closed pipe some detail",
        )
        with pytest.raises(mab.ExternalServiceError):
            mab.run_cmd(["buildah", "bud", "-t", "myimage:latest", "."])

    @patch("subprocess.run")
    def test_opm_error_with_regex_match_raises_iib_error(self, mock_run):
        mock_run.return_value = _make_completed_process(
            returncode=1,
            stderr="Error: invalid argument provided",
        )
        with pytest.raises(mab.IIBError, match="invalid argument provided"):
            mab.run_cmd(["/usr/bin/opm-v1.40.0", "serve", "."])

    @patch("subprocess.run")
    def test_opm_permissive_mode_disabled_raises_iib_error(self, mock_run):
        mock_run.return_value = _make_completed_process(
            returncode=1,
            stderr=(
                '"permissive mode disabled" error="error deleting packages from'
                ' database: error removing operator package somepackage"'
            ),
        )
        with pytest.raises(mab.IIBError, match="Error deleting packages from database"):
            mab.run_cmd(["/usr/bin/opm-v1.40.0", "serve", "."])

    @patch("subprocess.run")
    def test_unknown_command_failure_raises_generic_iib_error(self, mock_run):
        mock_run.return_value = _make_completed_process(returncode=1, stderr="some random error")
        with pytest.raises(mab.IIBError):
            mab.run_cmd(["unknowncmd", "arg"])


# ---------------------------------------------------------------------------
# generate_cache_locally
# ---------------------------------------------------------------------------


class TestGenerateCacheLocally:
    @patch(
        "multi_arch_builder.resolve_opm_binary_path",
        return_value="/usr/bin/opm-v1.40.0",
    )
    @patch("multi_arch_builder.run_cmd")
    def test_runs_opm_command_with_correct_args(self, mock_run_cmd, mock_resolve, tmp_path):
        fbc_dir = tmp_path / "fbc"
        fbc_dir.mkdir()
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        # Simulate opm populating the cache dir when run_cmd is called
        def side_effect(cmd, *args, **kwargs):
            (cache_dir / "generated.db").write_text("data")

        mock_run_cmd.side_effect = side_effect

        mab.generate_cache_locally(
            base_dir=str(tmp_path),
            fbc_dir=str(fbc_dir),
            local_cache_path=str(cache_dir),
            opm_version="v1.40.0",
        )

        mock_run_cmd.assert_called_once()
        cmd_arg = mock_run_cmd.call_args[0][0]
        assert cmd_arg[0] == "/usr/bin/opm-v1.40.0"
        assert "serve" in cmd_arg
        assert "--cache-only" in cmd_arg
        assert f"--cache-dir={cache_dir}" in cmd_arg

    @patch(
        "multi_arch_builder.resolve_opm_binary_path",
        return_value="/usr/bin/opm-v1.40.0",
    )
    @patch("multi_arch_builder.run_cmd")
    def test_cleans_existing_cache_directory_before_running(
        self, mock_run_cmd, mock_resolve, tmp_path
    ):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        stale_file = cache_dir / "stale.txt"
        stale_file.write_text("old data")
        stale_subdir = cache_dir / "subdir"
        stale_subdir.mkdir()

        # After run_cmd, simulate opm populating the cache
        def side_effect(*args, **kwargs):
            (cache_dir / "new.db").write_text("fresh")

        mock_run_cmd.side_effect = side_effect

        mab.generate_cache_locally(
            base_dir=str(tmp_path),
            fbc_dir=str(tmp_path / "fbc"),
            local_cache_path=str(cache_dir),
            opm_version="v1.40.0",
        )

        assert not stale_file.exists()
        assert not stale_subdir.exists()
        assert (cache_dir / "new.db").exists()

    @patch(
        "multi_arch_builder.resolve_opm_binary_path",
        return_value="/usr/bin/opm-v1.40.0",
    )
    @patch("multi_arch_builder.run_cmd")
    def test_raises_iib_error_when_cache_directory_empty_after_run(
        self, mock_run_cmd, mock_resolve, tmp_path
    ):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        # run_cmd does nothing → cache dir remains empty

        with pytest.raises(mab.IIBError, match="Cache directory is empty"):
            mab.generate_cache_locally(
                base_dir=str(tmp_path),
                fbc_dir=str(tmp_path / "fbc"),
                local_cache_path=str(cache_dir),
                opm_version="v1.40.0",
            )

    @patch(
        "multi_arch_builder.resolve_opm_binary_path",
        return_value="/usr/bin/opm-v1.40.0",
    )
    @patch("multi_arch_builder.run_cmd")
    def test_raises_iib_error_when_cache_directory_does_not_exist(
        self, mock_run_cmd, mock_resolve, tmp_path
    ):
        non_existent = str(tmp_path / "no_such_cache")

        with pytest.raises(mab.IIBError, match="Cannot access cache directory"):
            mab.generate_cache_locally(
                base_dir=str(tmp_path),
                fbc_dir=str(tmp_path / "fbc"),
                local_cache_path=non_existent,
                opm_version="v1.40.0",
            )

    @patch(
        "multi_arch_builder.resolve_opm_binary_path",
        return_value="/usr/bin/opm-v1.99.0",
    )
    @patch("multi_arch_builder.run_cmd")
    def test_uses_opm_version_argument(self, mock_run_cmd, mock_resolve, tmp_path):
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()

        def side_effect(cmd, *args, **kwargs):
            (cache_dir / "x").write_text("data")

        mock_run_cmd.side_effect = side_effect

        mab.generate_cache_locally(
            base_dir=str(tmp_path),
            fbc_dir=str(tmp_path / "fbc"),
            local_cache_path=str(cache_dir),
            opm_version="v1.99.0",
        )

        cmd_arg = mock_run_cmd.call_args[0][0]
        assert cmd_arg[0] == "/usr/bin/opm-v1.99.0"


# ---------------------------------------------------------------------------
# BuildConfig
# ---------------------------------------------------------------------------


class TestBuildConfig:
    def test_default_arch_map(self):
        cfg = mab.BuildConfig(
            image_name="img:tag",
            dockerfile_path="/Dockerfile",
            context_path="/ctx",
            platforms=["amd64"],
            labels=[],
            cache_dir="/cache",
            commit_sha="sha",
            opm_version="v1.40.0",
        )
        assert cfg.arch_map == {
            "amd64": "amd64",
            "arm64": "arm64",
            "ppc64le": "ppc64le",
            "s390x": "s390x",
        }

    def test_custom_arch_map(self):
        custom_map = {"amd64": "x86_64"}
        cfg = mab.BuildConfig(
            image_name="img:tag",
            dockerfile_path="/Dockerfile",
            context_path="/ctx",
            platforms=["amd64"],
            labels=[],
            cache_dir="/cache",
            commit_sha="sha",
            opm_version="v1.40.0",
            arch_map=custom_map,
        )
        assert cfg.arch_map == custom_map


# ---------------------------------------------------------------------------
# MultiArchBuilder.validate_dockerfile
# ---------------------------------------------------------------------------


class TestValidateDockerfile:
    def test_returns_true_when_dockerfile_exists(self, builder, tmp_path):
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM scratch\n")
        builder.config.dockerfile_path = str(dockerfile)
        assert builder.validate_dockerfile() is True

    def test_returns_false_when_dockerfile_missing(self, builder, tmp_path):
        builder.config.dockerfile_path = str(tmp_path / "nonexistent_Dockerfile")
        assert builder.validate_dockerfile() is False


# ---------------------------------------------------------------------------
# MultiArchBuilder._update_ca_trust
# ---------------------------------------------------------------------------


class TestUpdateCATrust:
    @patch("multi_arch_builder.run_cmd")
    def test_skips_when_bundle_path_missing(self, mock_run_cmd, builder, tmp_path):
        builder._update_ca_trust(str(tmp_path / "no_bundle.crt"))
        mock_run_cmd.assert_not_called()

    @patch("multi_arch_builder.run_cmd")
    def test_copies_and_updates_when_bundle_exists(self, mock_run_cmd, builder, tmp_path):
        bundle = tmp_path / "ca.crt"
        bundle.write_text("cert data")
        builder._update_ca_trust(str(bundle))
        assert mock_run_cmd.call_count == 2
        first_cmd = mock_run_cmd.call_args_list[0][0][0]
        assert first_cmd[0] == "cp"
        second_cmd = mock_run_cmd.call_args_list[1][0][0]
        assert second_cmd[0] == "update-ca-trust"

    @patch("multi_arch_builder.run_cmd", side_effect=mab.IIBError("copy failed"))
    def test_re_raises_iib_error(self, mock_run_cmd, builder, tmp_path):
        bundle = tmp_path / "ca.crt"
        bundle.write_text("cert")
        with pytest.raises(mab.IIBError, match="copy failed"):
            builder._update_ca_trust(str(bundle))


# ---------------------------------------------------------------------------
# MultiArchBuilder._verify_image_architecture
# ---------------------------------------------------------------------------


class TestVerifyImageArchitecture:
    @patch("multi_arch_builder.run_cmd")
    def test_passes_when_architecture_matches(self, mock_run_cmd, builder):
        mock_run_cmd.return_value = json.dumps({"Architecture": "amd64"})
        # Should not raise
        builder._verify_image_architecture("quay.io/org/img:latest-amd64", "amd64")

    @patch("multi_arch_builder.run_cmd")
    def test_raises_external_service_error_on_arch_mismatch(self, mock_run_cmd, builder):
        mock_run_cmd.return_value = json.dumps({"Architecture": "arm64"})
        with pytest.raises(mab.ExternalServiceError, match="Wrong arch created"):
            builder._verify_image_architecture("quay.io/org/img:latest-amd64", "amd64")

    @patch("multi_arch_builder.run_cmd")
    def test_skips_check_when_architecture_key_missing(self, mock_run_cmd, builder):
        mock_run_cmd.return_value = json.dumps({})
        # Should log a warning but not raise
        builder._verify_image_architecture("quay.io/org/img:latest-amd64", "amd64")

    @patch("multi_arch_builder.run_cmd")
    def test_uses_arch_map_for_comparison(self, mock_run_cmd, builder):
        """Custom arch_map should be respected when comparing architectures."""
        builder.config.arch_map = {"amd64": "x86_64"}
        mock_run_cmd.return_value = json.dumps({"Architecture": "x86_64"})
        # Should not raise — x86_64 is the mapped value for amd64
        builder._verify_image_architecture("quay.io/org/img:latest-amd64", "amd64")

    @patch("multi_arch_builder.run_cmd")
    def test_inspect_command_uses_containers_storage_scheme(self, mock_run_cmd, builder):
        mock_run_cmd.return_value = json.dumps({"Architecture": "arm64"})
        image = "quay.io/org/img:latest-arm64"
        try:
            builder._verify_image_architecture(image, "arm64")
        except Exception:
            pass
        cmd = mock_run_cmd.call_args[0][0]
        assert f"containers-storage:{image}" in cmd


# ---------------------------------------------------------------------------
# MultiArchBuilder._build_image
# ---------------------------------------------------------------------------


class TestBuildImage:
    @patch("multi_arch_builder.MultiArchBuilder._verify_image_architecture")
    @patch("multi_arch_builder.run_cmd")
    def test_executes_buildah_bud_with_correct_flags(self, mock_run_cmd, mock_verify, builder):
        builder._build_image("amd64", "quay.io/org/img:latest-amd64")
        cmd = mock_run_cmd.call_args[0][0]
        assert cmd[0] == "buildah"
        assert "bud" in cmd
        assert "--override-arch" in cmd
        assert "--arch" in cmd
        assert "-t" in cmd
        assert "quay.io/org/img:latest-amd64" in cmd

    @patch("multi_arch_builder.MultiArchBuilder._verify_image_architecture")
    @patch("multi_arch_builder.run_cmd")
    def test_adds_labels_to_buildah_command(self, mock_run_cmd, mock_verify, builder):
        builder.config.labels = ["version=1.0", "maintainer=team"]
        builder._build_image("amd64", "quay.io/org/img:latest-amd64")
        cmd = mock_run_cmd.call_args[0][0]
        label_indices = [i for i, v in enumerate(cmd) if v == "--label"]
        assert len(label_indices) == 2
        assert cmd[label_indices[0] + 1] == "version=1.0"
        assert cmd[label_indices[1] + 1] == "maintainer=team"

    @patch("multi_arch_builder.MultiArchBuilder._verify_image_architecture")
    @patch("multi_arch_builder.run_cmd")
    def test_appends_context_path(self, mock_run_cmd, mock_verify, builder):
        builder.config.context_path = "/tmp/ctx"
        builder._build_image("amd64", "quay.io/org/img:latest-amd64")
        cmd = mock_run_cmd.call_args[0][0]
        assert cmd[-1] == "/tmp/ctx"

    @patch("multi_arch_builder.MultiArchBuilder._verify_image_architecture")
    @patch("multi_arch_builder.run_cmd")
    def test_adds_binary_image_build_arg(self, mock_run_cmd, mock_verify, builder):
        builder.config.binary_image = "quay.io/binary@sha256:abc"
        builder._build_image("amd64", "quay.io/org/img:latest-amd64")
        cmd = mock_run_cmd.call_args[0][0]
        assert "--build-arg" in cmd
        assert "BINARY_IMAGE=quay.io/binary@sha256:abc" in cmd

    @patch("multi_arch_builder.MultiArchBuilder._verify_image_architecture")
    @patch("multi_arch_builder.run_cmd")
    def test_omits_binary_image_build_arg_when_unset(self, mock_run_cmd, mock_verify, builder):
        builder.config.binary_image = ""
        builder._build_image("amd64", "quay.io/org/img:latest-amd64")
        cmd = mock_run_cmd.call_args[0][0]
        assert "BINARY_IMAGE=" not in cmd

    @patch("multi_arch_builder.MultiArchBuilder._verify_image_architecture")
    @patch("multi_arch_builder.run_cmd", side_effect=mab.ExternalServiceError("503"))
    def test_raises_external_service_error_on_network_failure(
        self, mock_run_cmd, mock_verify, builder
    ):
        with pytest.raises(mab.ExternalServiceError):
            builder._build_image.__wrapped__(builder, "amd64", "quay.io/org/img:latest-amd64")


# ---------------------------------------------------------------------------
# MultiArchBuilder.build_all – catalog dir permission normalization
# ---------------------------------------------------------------------------


class TestBuildAllNormalizesConfigsPermissions:
    """Dockerfile COPY normalises permissions: root dirs become 0755.

    opm includes file and directory modes in its cache digest, so every
    entry under ``configs/`` must be normalised before cache generation
    to match what the runtime image will see after COPY.
    """

    @patch("multi_arch_builder.MultiArchBuilder._create_and_push_manifest_list")
    @patch("multi_arch_builder.MultiArchBuilder._build_image")
    @patch("multi_arch_builder.generate_cache_locally")
    @patch("multi_arch_builder.MultiArchBuilder._prepare_system")
    @patch("multi_arch_builder.run_cmd")
    def test_catalog_tree_normalised_before_cache_generation(
        self,
        mock_run_cmd,
        mock_prepare,
        mock_gen_cache,
        mock_build,
        mock_manifest,
        tmp_path,
    ):
        context = tmp_path / "ctx"
        context.mkdir()
        catalog = context / "configs"
        catalog.mkdir(mode=0o775)
        subdir = catalog / "operator-a"
        subdir.mkdir(mode=0o775)
        gitkeep = catalog / ".gitkeep"
        gitkeep.touch()
        gitkeep.chmod(0o664)
        catalog_json = subdir / "catalog.json"
        catalog_json.write_text("{}")
        catalog_json.chmod(0o664)

        # Simulate OpenShift emptyDir with setgid (mode 2777)
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir(mode=0o2777)

        dockerfile = context / "Dockerfile"
        dockerfile.write_text("FROM scratch\n")

        cfg = mab.BuildConfig(
            image_name="quay.io/org/img:latest",
            dockerfile_path=str(dockerfile),
            context_path=str(context),
            platforms=["amd64"],
            labels=[],
            cache_dir=str(cache_dir),
            commit_sha="abc123",
            opm_version="v1.40.0",
        )
        builder = mab.MultiArchBuilder(cfg)

        observed = {}

        def capture_modes(*args, **kwargs):
            observed["catalog_dir"] = catalog.stat().st_mode & 0o7777
            observed["subdir"] = subdir.stat().st_mode & 0o7777
            observed["gitkeep"] = gitkeep.stat().st_mode & 0o7777
            observed["catalog_json"] = catalog_json.stat().st_mode & 0o7777
            observed["cache_dir"] = cache_dir.stat().st_mode & 0o7777
            current_umask = os.umask(0)
            os.umask(current_umask)
            observed["umask"] = current_umask
            (cache_dir / "cache").mkdir(exist_ok=True)
            (cache_dir / "cache" / "packages.json").write_text("{}")
            (cache_dir / "digest").write_text("abc")

        mock_gen_cache.side_effect = capture_modes
        mock_run_cmd.return_value = '{"Digest": "sha256:abc"}'

        umask_before = os.umask(0o022)
        os.umask(umask_before)

        builder.build_all()

        umask_after = os.umask(0o022)
        os.umask(umask_after)
        assert umask_after == umask_before, (
            f"umask not restored after build_all(): was {umask_before:04o}, now {umask_after:04o}"
        )

        assert observed["catalog_dir"] == 0o755
        assert observed["subdir"] == 0o755
        assert observed["gitkeep"] == 0o644
        assert observed["catalog_json"] == 0o644
        assert observed["cache_dir"] == 0o755, (
            f"cache_dir should be 0755 (setgid stripped), got {observed['cache_dir']:04o}"
        )
        assert observed["umask"] == 0o022, (
            f"umask should be 0022 during cache generation, got {observed['umask']:04o}"
        )


# ---------------------------------------------------------------------------
# MultiArchBuilder._create_and_push_manifest_list
# ---------------------------------------------------------------------------


class TestCreateAndPushManifestList:
    @patch("multi_arch_builder.run_cmd")
    def test_creates_and_pushes_manifest_for_each_tag(self, mock_run_cmd, builder):
        """With a commit_sha, two tags should be created and pushed."""
        builder.config.image_name = "quay.io/org/img:latest"
        builder.config.commit_sha = "abc123"
        platform_images = [
            "quay.io/org/img:latest-amd64",
            "quay.io/org/img:latest-arm64",
        ]
        # First call (rm) raises IIBError("Manifest list not found locally.") → tolerated

        def side_effect(cmd, *args, **kwargs):
            if "rm" in cmd:
                raise mab.IIBError("Manifest list not found locally.")
            return ""

        mock_run_cmd.side_effect = side_effect
        # Should not raise
        builder._create_and_push_manifest_list.__wrapped__(builder, platform_images)

        # Expect: for each of 2 tags → rm + create + 2×add + push = 5 calls × 2 = 10
        assert mock_run_cmd.call_count == 10

    @patch("multi_arch_builder.run_cmd")
    def test_removes_existing_manifest_before_creating(self, mock_run_cmd, builder):
        builder.config.image_name = "quay.io/org/img:latest"
        builder.config.commit_sha = ""

        rm_attempted = []

        def side_effect(cmd, *args, **kwargs):
            if "rm" in cmd:
                rm_attempted.append(True)
                raise mab.IIBError("Manifest list not found locally.")
            return ""

        mock_run_cmd.side_effect = side_effect
        builder._create_and_push_manifest_list.__wrapped__(
            builder, ["quay.io/org/img:latest-amd64"]
        )
        assert rm_attempted

    @patch("multi_arch_builder.run_cmd")
    def test_unexpected_rm_error_re_raises(self, mock_run_cmd, builder):
        builder.config.image_name = "quay.io/org/img:latest"
        builder.config.commit_sha = ""

        def side_effect(cmd, *args, **kwargs):
            if "rm" in cmd:
                raise mab.IIBError("permission denied")
            return ""

        mock_run_cmd.side_effect = side_effect
        with pytest.raises(mab.IIBError, match="Error removing local manifest list"):
            builder._create_and_push_manifest_list.__wrapped__(
                builder, ["quay.io/org/img:latest-amd64"]
            )

    @patch("multi_arch_builder.run_cmd")
    def test_push_uses_docker_scheme_and_all_flag(self, mock_run_cmd, builder):
        builder.config.image_name = "quay.io/org/img:v1"
        builder.config.commit_sha = ""

        push_cmds = []

        def side_effect(cmd, *args, **kwargs):
            if "push" in cmd:
                push_cmds.append(cmd)
            if "rm" in cmd:
                raise mab.IIBError("Manifest list not found locally.")
            return ""

        mock_run_cmd.side_effect = side_effect
        builder._create_and_push_manifest_list.__wrapped__(builder, ["quay.io/org/img:v1-amd64"])

        assert push_cmds, "push was never called"
        push_cmd = push_cmds[0]
        assert "--all" in push_cmd
        assert "docker://quay.io/org/img:v1" in push_cmd


# ---------------------------------------------------------------------------
# IIB build metadata helpers
# ---------------------------------------------------------------------------


class TestResolveIibBuildMetadataPath:
    def test_relative_path_is_resolved_against_context(self, tmp_path):
        context = tmp_path / "ctx"
        context.mkdir()
        resolved = mab.resolve_iib_build_metadata_path(str(context), ".iib-build-metadata.json")
        assert resolved == context / ".iib-build-metadata.json"

    def test_absolute_path_is_unchanged(self, tmp_path):
        absolute = tmp_path / "custom" / "metadata.json"
        absolute.parent.mkdir()
        resolved = mab.resolve_iib_build_metadata_path(str(tmp_path / "ctx"), str(absolute))
        assert resolved == absolute


class TestLoadIibBuildMetadata:
    def test_loads_valid_json(self, tmp_path):
        metadata_file = tmp_path / ".iib-build-metadata.json"
        metadata_file.write_text('{"opm_version": "v1.48.0"}', encoding="utf-8")
        assert mab.load_iib_build_metadata(metadata_file) == {"opm_version": "v1.48.0"}

    def test_raises_when_file_missing(self, tmp_path):
        with pytest.raises(mab.IIBError, match="not found"):
            mab.load_iib_build_metadata(tmp_path / "missing.json")

    def test_raises_on_invalid_json(self, tmp_path):
        metadata_file = tmp_path / ".iib-build-metadata.json"
        metadata_file.write_text("{not json", encoding="utf-8")
        with pytest.raises(mab.IIBError, match="Invalid JSON"):
            mab.load_iib_build_metadata(metadata_file)

    def test_raises_when_root_is_not_object(self, tmp_path):
        metadata_file = tmp_path / ".iib-build-metadata.json"
        metadata_file.write_text('["array"]', encoding="utf-8")
        with pytest.raises(mab.IIBError, match="must be a JSON object"):
            mab.load_iib_build_metadata(metadata_file)


class TestOpmVersionFromMetadata:
    def test_strips_opm_prefix(self):
        assert mab.opm_version_from_metadata({"opm_version": "opm-v1.48.0"}) == "v1.48.0"

    def test_returns_version_without_prefix(self):
        assert mab.opm_version_from_metadata({"opm_version": "v1.40.0"}) == "v1.40.0"

    def test_maps_bare_opm_to_default(self):
        assert mab.opm_version_from_metadata({"opm_version": "opm"}) == mab.DEFAULT_OPM_VERSION

    def test_raises_when_opm_version_empty(self):
        with pytest.raises(mab.IIBError, match="must not be empty"):
            mab.opm_version_from_metadata({"opm_version": ""})

    def test_raises_when_opm_version_whitespace_only(self):
        with pytest.raises(mab.IIBError, match="must not be empty"):
            mab.opm_version_from_metadata({"opm_version": "   "})

    def test_returns_none_when_missing(self):
        assert mab.opm_version_from_metadata({}) is None


class TestResolveOpmBinaryPath:
    @patch.object(mab.Path, "is_file", return_value=True)
    def test_returns_path_when_binary_exists(self, mock_is_file):
        assert mab.resolve_opm_binary_path("v1.48.0") == "/usr/bin/opm-v1.48.0"
        mock_is_file.assert_called_once()

    @patch.object(mab.Path, "is_file", return_value=False)
    def test_raises_when_binary_missing(self, mock_is_file):
        with pytest.raises(mab.IIBError, match="OPM binary not found"):
            mab.resolve_opm_binary_path("v9.99.9")


class TestLabelsFromMetadata:
    def test_converts_dict_to_key_value_list(self, sample_iib_metadata):
        labels = mab.labels_from_metadata(sample_iib_metadata)
        assert labels == [
            "com.redhat.index.delivery.version=v4.19",
            "com.redhat.index.delivery.distribution_scope=prod",
        ]

    def test_returns_none_when_missing(self):
        assert mab.labels_from_metadata({}) is None

    def test_raises_when_labels_not_object(self):
        with pytest.raises(mab.IIBError, match="Invalid labels"):
            mab.labels_from_metadata({"labels": "bad"})


class TestArchesFromMetadata:
    def test_returns_arch_list(self, sample_iib_metadata):
        assert mab.arches_from_metadata(sample_iib_metadata) == ["amd64"]

    def test_returns_none_when_missing(self):
        assert mab.arches_from_metadata({}) is None

    def test_raises_when_arches_not_array(self):
        with pytest.raises(mab.IIBError, match="Invalid arches"):
            mab.arches_from_metadata({"arches": "amd64"})

    def test_raises_when_arches_empty(self):
        with pytest.raises(mab.IIBError, match="at least one architecture"):
            mab.arches_from_metadata({"arches": []})


class TestBinaryImageFromMetadata:
    def test_returns_image_reference(self, sample_iib_metadata):
        image = mab.binary_image_from_metadata(sample_iib_metadata)
        assert image.startswith("quay.io/operator-framework/")

    def test_returns_none_when_missing(self):
        assert mab.binary_image_from_metadata({}) is None

    def test_raises_when_not_string(self):
        with pytest.raises(mab.IIBError, match="Invalid binary_image"):
            mab.binary_image_from_metadata({"binary_image": 123})

    def test_raises_when_empty_string(self):
        with pytest.raises(mab.IIBError, match="must not be empty"):
            mab.binary_image_from_metadata({"binary_image": "   "})


# ---------------------------------------------------------------------------
# load_config_from_env
# ---------------------------------------------------------------------------


class TestLoadConfigFromEnv:
    def test_loads_values_from_metadata_file(self, metadata_build_context):
        cfg = mab.load_config_from_env()
        assert cfg.image_name == "quay.io/org/index:v1.0"
        assert cfg.commit_sha == "abc123def456"
        assert cfg.opm_version == "v1.48.0"
        assert cfg.platforms == ["amd64"]
        assert cfg.labels == [
            "com.redhat.index.delivery.version=v4.19",
            "com.redhat.index.delivery.distribution_scope=prod",
        ]
        assert cfg.binary_image.startswith("quay.io/operator-framework/")

    def test_custom_metadata_file_path(self, metadata_build_context, sample_iib_metadata):
        custom = metadata_build_context / "meta" / "build.json"
        custom.parent.mkdir()
        custom.write_text(json.dumps(sample_iib_metadata), encoding="utf-8")
        cfg = mab.load_config_from_env(metadata_file_path="meta/build.json")
        assert cfg.opm_version == "v1.48.0"

    def test_metadata_file_path_from_env(self, metadata_build_context, monkeypatch):
        monkeypatch.setenv(
            "IIB_BUILD_METADATA_FILE_PATH",
            ".iib-build-metadata.json",
        )
        cfg = mab.load_config_from_env()
        assert cfg.opm_version == "v1.48.0"

    def test_relative_context_is_joined_with_source_dir(self, tmp_path, monkeypatch):
        source = tmp_path / "source"
        ctx = source / "myapp"
        ctx.mkdir(parents=True)
        (ctx / ".iib-build-metadata.json").write_text(
            json.dumps({"opm_version": "v1.40.0", "arches": ["amd64"]}),
            encoding="utf-8",
        )
        monkeypatch.setenv("IMAGE", "quay.io/org/img:latest")
        monkeypatch.setenv("COMMIT_SHA", "sha123")
        monkeypatch.setenv("CONTEXT", "myapp")
        monkeypatch.setenv("DOCKERFILE", "./Dockerfile")

        real_join = os.path.join

        def join_under_tekton_source(first, *rest):
            if first == "/var/workdir/source" and rest:
                return str(source / rest[0].lstrip("./"))
            return real_join(first, *rest)

        monkeypatch.setattr(os.path, "join", join_under_tekton_source)

        cfg = mab.load_config_from_env()
        assert cfg.context_path == str(source / "myapp")

    def test_absolute_context_is_used_as_is(self, metadata_build_context, monkeypatch):
        monkeypatch.setenv("CONTEXT", str(metadata_build_context))
        cfg = mab.load_config_from_env()
        assert cfg.context_path == str(metadata_build_context)

    def test_relative_dockerfile_is_joined_with_source_dir(self, monkeypatch, tmp_path):
        source = tmp_path / "source"
        ctx = source / "app"
        ctx.mkdir(parents=True)
        (ctx / ".iib-build-metadata.json").write_text(
            json.dumps({"opm_version": "v1.40.0", "arches": ["amd64"]}),
            encoding="utf-8",
        )
        monkeypatch.setenv("IMAGE", "quay.io/org/img:latest")
        monkeypatch.setenv("COMMIT_SHA", "sha123")
        monkeypatch.setenv("CONTEXT", str(ctx))
        monkeypatch.setenv("DOCKERFILE", "./index.Dockerfile")

        cfg = mab.load_config_from_env()
        assert cfg.dockerfile_path == "/var/workdir/source/./index.Dockerfile"

    def test_default_cache_dir(self, metadata_build_context, monkeypatch):
        monkeypatch.delenv("CACHE_DIR", raising=False)
        cfg = mab.load_config_from_env()
        assert cfg.cache_dir == "/var/workdir/cache"

    def test_raises_when_opm_version_missing(self, tmp_path, monkeypatch):
        context = tmp_path / "ctx"
        context.mkdir()
        (context / ".iib-build-metadata.json").write_text(
            json.dumps({"arches": ["amd64"]}),
            encoding="utf-8",
        )
        monkeypatch.setenv("IMAGE", "quay.io/org/img:latest")
        monkeypatch.setenv("COMMIT_SHA", "sha123")
        monkeypatch.setenv("CONTEXT", str(context))

        with pytest.raises(mab.IIBError, match="opm_version is required"):
            mab.load_config_from_env()

    def test_raises_when_opm_version_empty(self, tmp_path, monkeypatch):
        context = tmp_path / "ctx"
        context.mkdir()
        (context / ".iib-build-metadata.json").write_text(
            json.dumps({"opm_version": "", "arches": ["amd64"]}),
            encoding="utf-8",
        )
        monkeypatch.setenv("IMAGE", "quay.io/org/img:latest")
        monkeypatch.setenv("COMMIT_SHA", "sha123")
        monkeypatch.setenv("CONTEXT", str(context))

        with pytest.raises(mab.IIBError, match="must not be empty"):
            mab.load_config_from_env()

    def test_raises_when_arches_missing(self, tmp_path, monkeypatch):
        context = tmp_path / "ctx"
        context.mkdir()
        (context / ".iib-build-metadata.json").write_text(
            json.dumps({"opm_version": "v1.40.0"}),
            encoding="utf-8",
        )
        monkeypatch.setenv("IMAGE", "quay.io/org/img:latest")
        monkeypatch.setenv("COMMIT_SHA", "sha123")
        monkeypatch.setenv("CONTEXT", str(context))

        with pytest.raises(mab.IIBError, match="arches is required"):
            mab.load_config_from_env()


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


class TestMain:
    @patch("multi_arch_builder.MultiArchBuilder.build_all")
    @patch("multi_arch_builder.load_config_from_env")
    @patch("sys.argv", ["multi-arch-builder.py"])
    def test_exits_with_code_1_when_image_name_missing(self, mock_load_config, mock_build_all):
        mock_load_config.return_value = mab.BuildConfig(
            image_name="",  # missing
            dockerfile_path="/Dockerfile",
            context_path="/ctx",
            platforms=["amd64"],
            labels=[],
            cache_dir="/cache",
            commit_sha="sha",
            opm_version="v1.40.0",
        )
        with pytest.raises(SystemExit) as exc_info:
            mab.main()
        assert exc_info.value.code == 1
        mock_build_all.assert_not_called()

    @patch("multi_arch_builder.MultiArchBuilder.build_all")
    @patch("multi_arch_builder.load_config_from_env")
    @patch("sys.argv", ["multi-arch-builder.py"])
    def test_exits_with_code_1_when_commit_sha_missing(self, mock_load_config, mock_build_all):
        mock_load_config.return_value = mab.BuildConfig(
            image_name="quay.io/org/img:latest",
            dockerfile_path="/Dockerfile",
            context_path="/ctx",
            platforms=["amd64"],
            labels=[],
            cache_dir="/cache",
            commit_sha="",  # missing
            opm_version="v1.40.0",
        )
        with pytest.raises(SystemExit) as exc_info:
            mab.main()
        assert exc_info.value.code == 1

    @patch("builtins.print")
    @patch("multi_arch_builder.MultiArchBuilder.build_all")
    @patch("multi_arch_builder.load_config_from_env")
    @patch("sys.argv", ["multi-arch-builder.py"])
    def test_prints_results_to_stdout_when_no_output_arg(
        self, mock_load_config, mock_build_all, mock_print
    ):
        mock_load_config.return_value = mab.BuildConfig(
            image_name="quay.io/org/img:latest",
            dockerfile_path="/Dockerfile",
            context_path="/ctx",
            platforms=["amd64"],
            labels=[],
            cache_dir="/cache",
            commit_sha="abc123",
            opm_version="v1.40.0",
        )
        mock_build_all.return_value = {"image_name": "quay.io/org/img:latest"}
        mab.main()
        mock_print.assert_called_once()
        printed = mock_print.call_args[0][0]
        assert "image_name" in printed

    @patch("multi_arch_builder.MultiArchBuilder.build_all")
    @patch("multi_arch_builder.load_config_from_env")
    @patch(
        "sys.argv",
        ["multi-arch-builder.py", "--output", "/tmp/results.json"],
    )
    def test_writes_results_to_file_when_output_arg_given(
        self, mock_load_config, mock_build_all, tmp_path
    ):
        output_file = tmp_path / "results.json"
        mock_load_config.return_value = mab.BuildConfig(
            image_name="quay.io/org/img:latest",
            dockerfile_path="/Dockerfile",
            context_path="/ctx",
            platforms=["amd64"],
            labels=[],
            cache_dir="/cache",
            commit_sha="abc123",
            opm_version="v1.40.0",
        )
        mock_build_all.return_value = {"image_name": "quay.io/org/img:latest"}
        with patch("sys.argv", ["multi-arch-builder.py", "--output", str(output_file)]):
            mab.main()
        assert output_file.exists()
        data = json.loads(output_file.read_text())
        assert data["image_name"] == "quay.io/org/img:latest"

    @patch(
        "multi_arch_builder.MultiArchBuilder.build_all",
        side_effect=mab.IIBError("boom"),
    )
    @patch("multi_arch_builder.load_config_from_env")
    @patch("sys.argv", ["multi-arch-builder.py"])
    def test_exits_with_code_1_on_iib_error(self, mock_load_config, mock_build_all):
        mock_load_config.return_value = mab.BuildConfig(
            image_name="quay.io/org/img:latest",
            dockerfile_path="/Dockerfile",
            context_path="/ctx",
            platforms=["amd64"],
            labels=[],
            cache_dir="/cache",
            commit_sha="sha",
            opm_version="v1.40.0",
        )
        with pytest.raises(SystemExit) as exc_info:
            mab.main()
        assert exc_info.value.code == 1
