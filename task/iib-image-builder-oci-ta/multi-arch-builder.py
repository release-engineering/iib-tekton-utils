#!/usr/bin/env python3
"""
Multi-architecture container builder with retry logic and cache management.
This script orchestrates buildah operations for building multi-arch images.
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

DEFAULT_IIB_BUILD_METADATA_FILE_PATH = ".iib-build-metadata.json"
# OPM binaries bundled in Containerfile.iib-build-task (keep in sync).
BUNDLED_OPM_VERSIONS = ("v1.26.4", "v1.40.0", "v1.44.0", "v1.48.0")
DEFAULT_OPM_VERSION = BUNDLED_OPM_VERSIONS[-1]


class IIBBaseException(Exception):
    """The base class for all IIB exceptions."""


class IIBError(IIBBaseException):
    """Custom exception for IIB operations."""


class ExternalServiceError(IIBBaseException):
    """Exception for external service errors."""


def _regex_reverse_search(
    regex: str,
    proc_response: subprocess.CompletedProcess,
) -> Optional[re.Match]:
    """
    Try to match the STDERR content with a regular expression from bottom to up.

    This is a complementary function for ``run_cmd``.

    :param str regex: The regular expression to try to match
    :param subprocess.CompletedProcess proc_response: the popen response to retrieve the STDERR from
    :return: the regex match or None if not matched
    :rtype: re.Match
    """
    # Start from the last log message since the failure occurs near the bottom
    for msg in reversed(proc_response.stderr.splitlines()):
        match = re.match(regex, msg)
        if match:
            return match
    return None


def run_cmd(
    cmd: List[str],
    params: Optional[Dict[str, Any]] = None,
    exc_msg: Optional[str] = None,
    strict: bool = True,
) -> str:
    """
    Run the given command with the provided parameters.

    :param list cmd: list of strings representing the command to be executed
    :param dict params: keyword parameters for command execution
    :param str exc_msg: an optional exception message when the command fails
    :param bool strict: when true function will throw exception when problem occurs
    :return: the command output
    :rtype: str
    :raises IIBError: if the command fails
    """
    exc_msg = exc_msg or "An unexpected error occurred"
    if not params:
        params = {}
    params.setdefault("universal_newlines", True)
    params.setdefault("encoding", "utf-8")
    params.setdefault("stderr", subprocess.PIPE)
    params.setdefault("stdout", subprocess.PIPE)

    logger.debug('Running the command "%s"', " ".join(cmd))
    response: subprocess.CompletedProcess = subprocess.run(cmd, **params)

    if strict and response.returncode != 0:
        if set(["buildah", "manifest", "rm"]) <= set(cmd) and "image not known" in response.stderr:
            raise IIBError("Manifest list not found locally.")
        logger.error('The command "%s" failed with: %s', " ".join(cmd), response.stderr)
        regex: str
        match: Optional[re.Match]
        if Path(cmd[0]).stem.startswith("opm"):
            # Capture the error message right before the help display
            regex = r"^(?:Error: )(.+)$"
            match = _regex_reverse_search(regex, response)
            if match:
                raise IIBError(f"{exc_msg.rstrip('.')}: {match.groups()[0]}")
            elif (
                '"permissive mode disabled" error="error deleting packages from'
                " database: error removing operator package" in response.stderr
            ):
                raise IIBError("Error deleting packages from database")
        elif cmd[0] == "buildah":
            # Check for HTTP 403 or 50X errors on buildah
            network_regexes = [
                r".*([e,E]rror:? creating build container).*(:?(403|50[0-9]|125)\s?.*$)",
                r".*(read\/write on closed pipe.*$)",
            ]
            for regex in network_regexes:
                match = _regex_reverse_search(regex, response)
                if match:
                    raise ExternalServiceError(f"{exc_msg}: {': '.join(match.groups()).strip()}")

        raise IIBError(exc_msg)

    return response.stdout


@dataclass
class BuildConfig:
    """Configuration for the multi-arch build."""

    image_name: str
    dockerfile_path: str
    context_path: str
    platforms: List[str]
    labels: List[str]
    cache_dir: str
    commit_sha: str
    opm_version: str
    binary_image: str = ""
    # Architecture mapping for platform names to expected architecture values
    arch_map: Dict[str, str] = field(
        default_factory=lambda: {
            "amd64": "amd64",
            "arm64": "arm64",
            "ppc64le": "ppc64le",
            "s390x": "s390x",
        }
    )


def resolve_iib_build_metadata_path(context_path: str, metadata_file_path: str) -> Path:
    """
    Resolve the IIB build metadata file path.

    Relative paths are resolved against the build context directory.

    :param str context_path: build context directory
    :param str metadata_file_path: path to the metadata file
    :return: absolute path to the metadata file
    :rtype: Path
    """
    path = Path(metadata_file_path)
    if path.is_absolute():
        return path
    return Path(context_path) / path


def load_iib_build_metadata(metadata_path: Path) -> Dict[str, Any]:
    """
    Load IIB build metadata from the configured metadata file.

    :param Path metadata_path: path to the metadata JSON file
    :return: parsed metadata as a dictionary
    :rtype: Dict[str, Any]
    :raises IIBError: if the file is missing or contains invalid JSON
    """
    if not metadata_path.is_file():
        raise IIBError(f"IIB build metadata file not found: {metadata_path}")

    logger.info("Loading IIB build metadata from %s", metadata_path)
    try:
        with open(metadata_path, encoding="utf-8") as metadata_file:
            metadata = json.load(metadata_file)
    except json.JSONDecodeError as exc:
        raise IIBError(f"Invalid JSON in {metadata_path}: {exc}") from exc

    if not isinstance(metadata, dict):
        raise IIBError(f"IIB build metadata must be a JSON object, got {type(metadata).__name__}")

    return metadata


def opm_version_from_metadata(metadata: Dict[str, Any]) -> Optional[str]:
    """
    Extract ``opm_version`` from IIB build metadata.

    Strips the ``opm-`` prefix when present (e.g. ``opm-v1.48.0`` -> ``v1.48.0``).
    The bare value ``opm`` (IIB's ``iib_default_opm``) is mapped to the latest
    bundled OPM version in the task image. A missing key returns ``None``; an
    empty or whitespace-only value raises ``IIBError``.

    :param dict metadata: IIB build metadata
    :return: normalized OPM version, or None if not set in metadata
    :rtype: Optional[str]
    :raises IIBError: if ``opm_version`` is present but empty or malformed
    """
    raw_version = metadata.get("opm_version")
    if raw_version is None:
        return None

    version = str(raw_version).strip()
    if not version:
        raise IIBError("Invalid opm_version in IIB build metadata file: value must not be empty")

    if version.startswith("opm-"):
        version = version.removeprefix("opm-")

    if not version:
        raise IIBError(
            "Invalid opm_version in IIB build metadata file: "
            f"no version after stripping opm- prefix from {raw_version!r}"
        )

    # IIB may write the default command name without a version suffix.
    if version == "opm":
        logger.warning(
            "opm_version in metadata is %r (IIB default); using bundled OPM %s",
            raw_version,
            DEFAULT_OPM_VERSION,
        )
        return DEFAULT_OPM_VERSION

    return version


def resolve_opm_binary_path(opm_version: str) -> str:
    """
    Resolve the filesystem path to the versioned ``opm`` binary.

    :param str opm_version: normalized OPM version (e.g. ``v1.48.0``)
    :return: absolute path to the ``opm`` binary
    :rtype: str
    :raises IIBError: if the requested version is not bundled in the task image
    """
    opm_binary = Path(f"/usr/bin/opm-{opm_version}")
    if opm_binary.is_file():
        return str(opm_binary)

    supported = ", ".join(BUNDLED_OPM_VERSIONS)
    raise IIBError(
        f"OPM binary not found at {opm_binary}. "
        f"Supported opm_version values: {supported} or opm-<version> "
        f"(e.g. opm-v1.48.0). Got opm_version={opm_version!r}."
    )


def labels_from_metadata(metadata: Dict[str, Any]) -> Optional[List[str]]:
    """
    Extract ``labels`` from IIB build metadata.

    :param dict metadata: IIB build metadata
    :return: list of ``key=value`` labels, or None if not set in metadata
    :rtype: Optional[List[str]]
    :raises IIBError: if ``labels`` is present but not a JSON object
    """
    raw_labels = metadata.get("labels")
    if raw_labels is None:
        return None

    if not isinstance(raw_labels, dict):
        raise IIBError(
            "Invalid labels in IIB build metadata file: "
            f"expected object, got {type(raw_labels).__name__}"
        )

    return [f"{key}={value}" for key, value in raw_labels.items()]


def arches_from_metadata(metadata: Dict[str, Any]) -> Optional[List[str]]:
    """
    Extract ``arches`` from IIB build metadata.

    :param dict metadata: IIB build metadata
    :return: list of platform/arch names, or None if not set in metadata
    :rtype: Optional[List[str]]
    :raises IIBError: if ``arches`` is present but invalid or empty
    """
    raw_arches = metadata.get("arches")
    if raw_arches is None:
        return None

    if not isinstance(raw_arches, list):
        raise IIBError(
            "Invalid arches in IIB build metadata file: "
            f"expected array, got {type(raw_arches).__name__}"
        )

    arches = [str(arch).strip() for arch in raw_arches if str(arch).strip()]
    if not arches:
        raise IIBError(
            "Invalid arches in IIB build metadata file: "
            "array must contain at least one architecture"
        )

    return arches


def binary_image_from_metadata(metadata: Dict[str, Any]) -> Optional[str]:
    """
    Extract ``binary_image`` from IIB build metadata.

    :param dict metadata: IIB build metadata
    :return: container image reference for the index binary image, or None if not set
    :rtype: Optional[str]
    :raises IIBError: if ``binary_image`` is present but invalid or empty
    """
    raw_binary_image = metadata.get("binary_image")
    if raw_binary_image is None:
        return None

    if not isinstance(raw_binary_image, str):
        raise IIBError(
            "Invalid binary_image in IIB build metadata file: "
            f"expected string, got {type(raw_binary_image).__name__}"
        )

    binary_image = raw_binary_image.strip()
    if not binary_image:
        raise IIBError("Invalid binary_image in IIB build metadata file: value must not be empty")

    return binary_image


def generate_cache_locally(
    base_dir: str,
    fbc_dir: str,
    local_cache_path: str,
    opm_version: str,
) -> None:
    """
    Generate the cache for the index image locally before building it.

    :param str base_dir: base directory where cache should be created.
    :param str fbc_dir: directory containing file-based catalog (JSON or YAML files).
    :param str local_cache_path: path to the locally generated cache.
    :param str opm_version: OPM version used to select the ``opm`` binary (e.g. ``v1.48.0``).
    :return: Returns path to generated cache
    :rtype: str
    :raises: IIBError when cache was not generated

    """
    opm_binary = resolve_opm_binary_path(opm_version)

    cmd = [
        opm_binary,
        "serve",
        os.path.abspath(fbc_dir),
        f"--cache-dir={local_cache_path}",
        "--cache-only",
        "--termination-log",
        "/dev/null",
    ]

    logger.info("Generating cache for the file-based catalog")

    # Clean up existing cache directory
    if os.path.exists(local_cache_path):
        # Remove contents but keep the directory structure because mount points cannot be removed
        for item in os.listdir(local_cache_path):
            item_path = os.path.join(local_cache_path, item)
            if os.path.isfile(item_path):
                os.remove(item_path)
            elif os.path.isdir(item_path):
                shutil.rmtree(item_path)

    # Run the opm command
    run_cmd(cmd, {"cwd": base_dir}, exc_msg="Failed to generate cache for file-based catalog")

    try:
        cache_contents = os.listdir(local_cache_path)
        if not cache_contents:
            error_msg = f"Cache directory is empty at {local_cache_path}"
            logger.error(error_msg)
            raise IIBError(error_msg)
        logger.info(f"✓ Cache generated successfully with {len(cache_contents)} items")
    except OSError as e:
        error_msg = f"Cannot access cache directory at {local_cache_path}: {e}"
        logger.error(error_msg)
        raise IIBError(error_msg)


class MultiArchBuilder:
    """Orchestrates multi-architecture container builds."""

    def __init__(self, config: BuildConfig):
        self.config = config

    def validate_dockerfile(self) -> bool:
        """
        Validate that Dockerfile exists.

        :return: True if Dockerfile exists, False otherwise
        :rtype: bool
        """
        if not Path(self.config.dockerfile_path).exists():
            logger.error(f"✗ Dockerfile not found: {self.config.dockerfile_path}")
            return False
        logger.info(f"✓ Dockerfile found: {self.config.dockerfile_path}")
        return True

    def _update_ca_trust(self, ca_bundle_path: str) -> None:
        """
        Update CA trust certificates.

        :param str ca_bundle_path: path to the CA bundle file
        :raises IIBError: if updating CA trust fails
        """
        if not Path(ca_bundle_path).exists():
            logger.warning(f"CA bundle not found at {ca_bundle_path}")
            return

        logger.info("Updating CA trust certificates")

        try:
            # Copy CA bundle to anchors directory
            run_cmd(["cp", "-vf", ca_bundle_path, "/etc/pki/ca-trust/source/anchors/"])

            # Update CA trust
            run_cmd(["update-ca-trust"])
            logger.info("✓ CA trust updated successfully")
        except IIBError as e:
            logger.error(f"✗ Failed to update CA trust: {e}")
            raise

    def _prepare_system(self) -> None:
        """
        Prepare the system for buildah operations.

        :raises IIBError: if system preparation fails
        """
        logger.info("Preparing system for buildah operations")

        try:
            # Fix permissions on /var/lib/containers
            run_cmd(["chown", "root:root", "/var/lib/containers"])

            # Configure short-name-mode
            run_cmd(
                [
                    "sed",
                    "-i",
                    r's/^\s*short-name-mode\s*=\s*.*/short-name-mode = "disabled"/',
                    "/etc/containers/registries.conf",
                ]
            )

            # Set up user namespace
            with open("/etc/subuid", "a") as f:
                f.write("root:1:4294967294\n")

            logger.info("✓ System prepared successfully")
        except IIBError as e:
            logger.error(f"✗ Failed to prepare system: {e}")
            raise

    @retry(
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
        retry=retry_if_exception_type(ExternalServiceError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2),
    )
    def _build_image(self, arch: str, destination: str) -> None:
        """
        Build the index image for the specified architecture.

        :param str arch: the architecture to build this image for
        :param str destination: the destination image name
        :raises IIBError: if the build fails
        """
        logger.info(
            "Building the container image with the %s dockerfile for arch %s and tagging it as %s",
            os.path.basename(self.config.dockerfile_path),
            arch,
            destination,
        )

        # Prepare buildah command with improved options
        cmd = [
            "buildah",
            "bud",
            "--no-cache",
            "--format",
            "docker",
            "--override-arch",
            arch,
            "--arch",
            arch,
            "--tls-verify=true",
            "--ulimit",
            "nofile=4096:4096",
            "-t",
            destination,
            "-f",
            self.config.dockerfile_path,
        ]

        # Add labels
        for label in self.config.labels:
            cmd.extend(["--label", label.strip()])

        if self.config.binary_image:
            cmd.extend(["--build-arg", f"BINARY_IMAGE={self.config.binary_image}"])

        # Add context
        cmd.append(self.config.context_path)

        # Execute build with retry logic
        run_cmd(cmd, {"timeout": 3600}, f"build for {arch} failed")

        # Verify architecture was set correctly
        logger.debug("Verifying that %s was built with expected arch %s", destination, arch)
        self._verify_image_architecture(destination, arch)

    def _verify_image_architecture(self, image_name: str, expected_arch: str) -> None:
        """
        Verify that the built image has the correct architecture using skopeo inspect.

        :param str image_name: the image name to verify
        :param str expected_arch: the expected architecture
        """

        # Get image architecture using skopeo inspect
        inspect_cmd = ["skopeo", "inspect", "--no-tags", f"containers-storage:{image_name}"]
        result = run_cmd(inspect_cmd, {"timeout": 60}, f"inspect {image_name} failed")
        image_data = json.loads(result)

        # Check architecture in image config
        arch = image_data.get("Architecture")

        if not arch:
            logger.warning(
                'The "Architecture" was not found in image metadata. '
                "Skipping the check that confirms if the architecture was set correctly."
            )
            return

        # Map of platform names to expected architecture values
        # TODO: move to config
        arch_map = self.config.arch_map

        expected_arch_value = arch_map.get(expected_arch, expected_arch)

        if arch != expected_arch_value:
            logger.warning("Wrong arch created for %s", image_name)
            raise ExternalServiceError(
                f"Wrong arch created, for image {image_name} "
                f"expected arch {expected_arch_value}, found {arch}"
            )

        logger.info(f"✓ Architecture verification passed for {image_name}: {arch}")

    @retry(
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
        retry=retry_if_exception_type(IIBError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2),
    )
    def _create_and_push_manifest_list(
        self,
        platform_images: List[str],
    ) -> None:
        """
        Create and push the manifest list to the configured registry.

        :param list platform_images: list of platform-specific image names
        :raises IIBError: if creating or pushing the manifest list fails
        """
        buildah_manifest_cmd = ["buildah", "manifest"]
        image_name_repo, image_name_tag = self.config.image_name.split(":", 1)
        # Initialize _tags with the output image tag
        _tags = [image_name_tag]
        if self.config.commit_sha:
            _tags.append(self.config.commit_sha)

        output_pull_specs = []
        for tag in _tags:
            output_pull_spec = f"{image_name_repo}:{tag}"
            output_pull_specs.append(output_pull_spec)
            try:
                run_cmd(
                    buildah_manifest_cmd + ["rm", output_pull_spec],
                    exc_msg=(
                        f"Failed to remove local manifest list. {output_pull_spec} does not exist"
                    ),
                )
            except IIBError as e:
                error_msg = str(e)
                if "Manifest list not found locally." not in error_msg:
                    raise IIBError(f"Error removing local manifest list: {error_msg}")
                logger.debug(
                    "Manifest list cannot be removed. No manifest list %s found", output_pull_spec
                )
            logger.info("Creating the manifest list %s locally", output_pull_spec)
            run_cmd(
                buildah_manifest_cmd + ["create", output_pull_spec],
                exc_msg=f"Failed to create the manifest list locally: {output_pull_spec}",
            )
            for arch_image in platform_images:
                run_cmd(
                    buildah_manifest_cmd + ["add", output_pull_spec, arch_image],
                    exc_msg=(
                        f"Failed to add {arch_image} to the local manifest list: {output_pull_spec}"
                    ),
                )

            logger.debug("Pushing manifest list %s", output_pull_spec)
            run_cmd(
                buildah_manifest_cmd
                + [
                    "push",
                    "--all",
                    "--format",
                    "v2s2",
                    "--tls-verify=true",
                    output_pull_spec,
                    f"docker://{output_pull_spec}",
                ],
                exc_msg=f"Failed to push the manifest list to {output_pull_spec}",
            )

    def build_all(self, ca_bundle_path: Optional[str] = None) -> Dict[str, Any]:
        """
        Build multi-arch image and return results.

        :param Optional[str] ca_bundle_path: path to CA bundle file for trust updates
        :return: dictionary containing build results including image name, digest, platforms, etc.
        :rtype: Dict[str, Any]
        :raises IIBError: if the build process fails
        :raises RuntimeError: if Dockerfile validation fails
        """
        logger.info("Starting multi-architecture build")

        # Validate Dockerfile exists
        if not self.validate_dockerfile():
            raise RuntimeError("Dockerfile validation failed")

        # Update CA trust if bundle provided
        if ca_bundle_path:
            self._update_ca_trust(ca_bundle_path)

        # Prepare system
        self._prepare_system()

        # Generate cache using OPM
        logger.info("Generating cache using OPM")
        catalog_dir = Path(self.config.context_path) / "configs"
        if not catalog_dir.exists():
            raise IIBError(f"Catalog directory not found at {catalog_dir}")

        logger.info(f"Found catalog directory at {catalog_dir}")
        generate_cache_locally(
            base_dir=self.config.context_path,
            fbc_dir=str(catalog_dir),
            local_cache_path=self.config.cache_dir,
            opm_version=self.config.opm_version,
        )

        # Copy cache into build context so Dockerfile can access it
        logger.info("Copying cache into build context")
        context_cache_dir = Path(self.config.context_path) / "cache"
        try:
            if context_cache_dir.exists():
                shutil.rmtree(context_cache_dir)
            shutil.copytree(self.config.cache_dir, context_cache_dir)
            logger.info(f"✓ Cache copied to {context_cache_dir}")
        except (OSError, IOError) as e:
            logger.error(f"Failed to copy cache to build context: {e}")
            raise IIBError(f"Failed to copy cache to build context: {e}")

        # Build images for each platform
        platform_images = []
        for platform in self.config.platforms:
            try:
                platform_clean = platform.strip()
                # output-image:tag-platform
                platform_image = f"{self.config.image_name}-{platform_clean}"
                self._build_image(platform_clean, platform_image)
                platform_images.append(platform_image)
            except (IIBError, ExternalServiceError) as e:
                logger.error(f"Failed to build for {platform}: {e}")
                raise

        # Create and push manifest
        logger.info("Creating and pushing multi-arch manifest")

        # Create and push manifest list
        self._create_and_push_manifest_list(platform_images)

        # Get manifest digest
        inspect_cmd = ["skopeo", "inspect", "--no-tags", f"docker://{self.config.image_name}"]
        result = run_cmd(inspect_cmd, {"timeout": 60}, "inspect manifest failed")
        manifest_data = json.loads(result)
        digest = manifest_data.get("Digest", "")

        results = {
            "image_name": self.config.image_name,
            "digest": digest,
            "platforms": self.config.platforms,
            "platform_images": platform_images,
            "opm_version": self.config.opm_version,
        }
        if self.config.binary_image:
            results["binary_image"] = self.config.binary_image
        return results


def load_config_from_env(metadata_file_path: Optional[str] = None) -> BuildConfig:
    """
    Load configuration from IIB build metadata and Tekton environment variables.

    Values defined in the IIB build metadata file (``opm_version``, ``labels``,
    ``arches``, ``binary_image``) are read only from that file. The file path is
    set via ``--iib-build-metadata-file-path`` or the ``IIB_BUILD_METADATA_FILE_PATH``
    environment variable. Environment variables are used for Tekton/task settings
    (``IMAGE``, ``COMMIT_SHA``, etc.).

    :param Optional[str] metadata_file_path: path to the metadata file (overrides env)
    :return: BuildConfig object populated from metadata and environment variables
    :rtype: BuildConfig
    """
    # Source code is extracted to /var/workdir/source by the use-trusted-artifact step
    source_dir = "/var/workdir/source"

    # If CONTEXT is relative, make it relative to source_dir
    context_path = os.environ.get("CONTEXT", ".")
    if not context_path.startswith("/"):
        context_path = os.path.join(source_dir, context_path)

    # If DOCKERFILE is relative, make it relative to source_dir
    dockerfile_path = os.environ.get("DOCKERFILE", "./Dockerfile")
    if not dockerfile_path.startswith("/"):
        dockerfile_path = os.path.join(source_dir, dockerfile_path)

    if metadata_file_path is None:
        metadata_file_path = (
            os.environ.get("IIB_BUILD_METADATA_FILE_PATH") or DEFAULT_IIB_BUILD_METADATA_FILE_PATH
        )
    metadata_file_path = metadata_file_path.strip() or DEFAULT_IIB_BUILD_METADATA_FILE_PATH
    logger.info("IIB build metadata file path: %s", metadata_file_path)

    metadata_path = resolve_iib_build_metadata_path(context_path, metadata_file_path)
    metadata = load_iib_build_metadata(metadata_path)

    raw_opm_version = metadata.get("opm_version")
    opm_version = opm_version_from_metadata(metadata)
    if opm_version is None:
        raise IIBError(f"opm_version is required in {metadata_path}")
    logger.info(
        "OPM version %s loaded from %s (raw: %r)",
        opm_version,
        metadata_path,
        raw_opm_version,
    )

    labels = labels_from_metadata(metadata)
    if labels is None:
        labels = []
    logger.info("Loaded %d label(s) from %s", len(labels), metadata_path)

    platforms = arches_from_metadata(metadata)
    if platforms is None:
        raise IIBError(f"arches is required in {metadata_path}")
    logger.info(
        "Platforms %s loaded from %s (arches)",
        ", ".join(platforms),
        metadata_path,
    )

    binary_image = binary_image_from_metadata(metadata) or ""
    if binary_image:
        logger.info("Binary image %s loaded from %s", binary_image, metadata_path)
    else:
        logger.info("No binary_image configured in %s", metadata_path)

    return BuildConfig(
        image_name=os.environ.get("IMAGE", ""),
        dockerfile_path=dockerfile_path,
        context_path=context_path,
        platforms=platforms,
        labels=labels,
        cache_dir=os.environ.get("CACHE_DIR", "/var/workdir/cache"),
        commit_sha=os.environ.get("COMMIT_SHA", ""),
        opm_version=opm_version,
        binary_image=binary_image,
    )


def main():
    """
    Main entry point for the multi-architecture container builder.

    Parses command line arguments, loads configuration from environment,
    and executes the build process.
    """
    parser = argparse.ArgumentParser(description="Multi-architecture container builder")
    parser.add_argument("--ca-bundle", help="Path to CA bundle file")
    parser.add_argument("--output", help="Path to output results JSON file")
    parser.add_argument(
        "--iib-build-metadata-file-path",
        default=os.environ.get(
            "IIB_BUILD_METADATA_FILE_PATH",
            DEFAULT_IIB_BUILD_METADATA_FILE_PATH,
        ),
        help=(
            "Path to IIB build metadata JSON file "
            "(relative to CONTEXT unless absolute; default: .iib-build-metadata.json)"
        ),
    )

    args = parser.parse_args()

    try:
        config = load_config_from_env(metadata_file_path=args.iib_build_metadata_file_path)

        # Validate required fields
        if not config.image_name or not config.commit_sha:
            raise ValueError("IMAGE name and COMMIT_SHA are required")

        # Create builder and run build
        builder = MultiArchBuilder(config)
        results = builder.build_all(args.ca_bundle)

        # Output results
        if args.output:
            with open(args.output, "w") as f:
                json.dump(results, f, indent=2)
            logger.info(f"Results written to: {args.output}")
        else:
            print(json.dumps(results, indent=2))

        logger.info("Multi-architecture build completed successfully")

    except (IIBError, ExternalServiceError, RuntimeError, ValueError) as e:
        logger.error(f"Build failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
