# iib-image-builder-oci-ta task

Tekton task that builds multi-architecture Operator Index Images from a Trusted Artifact using Python orchestration and buildah. It is designed for file-based catalogs (FBC) and follows the same build settings model as [IIB](https://github.com/release-engineering/iib).

## Overview

The task runs two steps:

1. **`use-trusted-artifact`** — extracts the application source from a Trusted Artifact URI into `/var/workdir/source`.
2. **`build-multi-arch`** — runs `multi-arch-builder.py`, which:
   - loads build settings from the IIB build metadata JSON file,
   - generates an OPM cache from the file-based catalog under `configs/`,
   - builds a per-architecture image with `buildah bud` for each target arch,
   - assembles and pushes a multi-architecture manifest list with `buildah manifest`.

Build settings `opm_version`, `labels`, `arches`, and `binary_image` are read **only** from the IIB build metadata file (default `.iib-build-metadata.json` in the build context). Override the path with the `IIB_BUILD_METADATA_FILE_PATH` parameter.

Tekton parameters and environment variables (`IMAGE`, `COMMIT_SHA`, `CONTEXT`, `DOCKERFILE`, …) control where and how the build runs, not the index content itself.

## Expected source layout

The Trusted Artifact should contain at least:

```
.
├── .iib-build-metadata.json   # IIB build settings (see below)
├── configs/                   # file-based catalog (JSON or YAML)
└── Dockerfile                 # index image Dockerfile (or path via DOCKERFILE param)
```

During the build, OPM writes a cache under `/var/workdir/cache`, then copies it into `<CONTEXT>/cache` so the Dockerfile can consume it.

## IIB build metadata

Place a JSON file in the build context (default: `.iib-build-metadata.json`). Relative paths in `IIB_BUILD_METADATA_FILE_PATH` are resolved against `CONTEXT`.

| Field | Required | Description |
|---|---|---|
| `opm_version` | yes | OPM version for cache generation. Accepts `v1.48.0`, `opm-v1.48.0`, or IIB's default `opm` (see [opm_version normalization](#opm_version-normalization) below). Must not be empty. |
| `arches` | yes | Target architectures, e.g. `["amd64", "arm64", "ppc64le", "s390x"]` |
| `labels` | no | Object of label key/value pairs applied to built images |
| `binary_image` | no | Passed to the Dockerfile as `BINARY_IMAGE` build arg |

Example:

```json
{
  "opm_version": "opm-v1.48.0",
  "arches": ["amd64", "arm64"],
  "labels": {
    "com.redhat.index.delivery.version": "v4.19",
    "com.redhat.index.delivery.distribution_scope": "prod"
  },
  "binary_image": "quay.io/operator-framework/upstream-registry-builder@sha256:7c8068817855b55e60ff5c2591c494130c2d105e0cc062836a5438a42935f8f8"
}
```

Supported OPM versions are bundled in the task image: `v1.26.4`, `v1.40.0`, `v1.44.0`, and `v1.48.0`.

### opm_version normalization

The builder resolves `opm_version` from metadata as follows:

| Metadata value | Result |
|---|---|
| Key missing | Build fails: `opm_version is required` |
| `""` or whitespace only | Build fails: `value must not be empty` |
| `"opm"` (IIB `iib_default_opm`) | Uses latest bundled version (`v1.48.0`) with a warning in the log |
| `"opm-v1.48.0"` | Strips the `opm-` prefix → `v1.48.0` |
| `"v1.48.0"` | Used as-is |

IIB often writes `"opm"` when no OCP-to-OPM mapping is configured. The task image has no unversioned `opm` binary in `PATH`; only versioned binaries under `/usr/bin/opm-<version>` are available. Prefer an explicit version in metadata when you know which OPM release the index requires.

## Parameters

| Name | Description | Default | Required |
|---|---|---|---|
| `IMAGE` | Image reference buildah will push | — | yes |
| `SOURCE_ARTIFACT` | Trusted Artifact URI with application source | — | yes |
| `COMMIT_SHA` | Commit SHA; added as an extra manifest tag when set | `""` | yes at runtime* |
| `CONTEXT` | Build context directory (relative to extracted source) | `.` | no |
| `DOCKERFILE` | Path to the Dockerfile (relative to extracted source) | `./Dockerfile` | no |
| `IIB_BUILD_METADATA_FILE_PATH` | Path to IIB build metadata JSON (relative to `CONTEXT` unless absolute) | `.iib-build-metadata.json` | no |
| `STORAGE_DRIVER` | buildah storage driver | `overlay` | no |
| `caTrustConfigMapName` | ConfigMap containing the CA bundle | `trusted-ca` | no |
| `caTrustConfigMapKey` | Key in the ConfigMap with CA bundle data | `ca-bundle.crt` | no |

\* `COMMIT_SHA` has an empty Tekton default but `multi-arch-builder.py` fails the build if it is not set.

## Results

| Name | Description |
|---|---|
| `IMAGE_DIGEST` | Digest of the pushed multi-arch manifest |
| `IMAGE_REF` | Full image reference with digest (`name@sha256:…`) |
| `IMAGE_URL` | Image repository and tag (`name:tag`) |

## Resource requirements

The task step template requests 4 CPU / 4 Gi memory and limits memory to 16 Gi. Multi-arch index builds with OPM cache generation are memory-intensive; adjust cluster quotas accordingly.

## Usage

### Basic TaskRun

```yaml
apiVersion: tekton.dev/v1
kind: TaskRun
metadata:
  name: build-multi-arch-index
spec:
  taskRef:
    name: iib-image-builder-oci-ta
  params:
    - name: IMAGE
      value: "quay.io/myorg/my-index:v1.0.0"
    - name: COMMIT_SHA
      value: "abc123def456"
    - name: SOURCE_ARTIFACT
      value: "oci://source-artifact"
```

### Custom context, Dockerfile, and metadata path

```yaml
apiVersion: tekton.dev/v1
kind: TaskRun
metadata:
  name: build-multi-arch-index-advanced
spec:
  taskRef:
    name: iib-image-builder-oci-ta
  params:
    - name: IMAGE
      value: "quay.io/myorg/my-index:v1.0.0"
    - name: COMMIT_SHA
      value: "abc123def456"
    - name: SOURCE_ARTIFACT
      value: "oci://source-artifact"
    - name: CONTEXT
      value: "./operator"
    - name: DOCKERFILE
      value: "./operator/index.Dockerfile"
    - name: IIB_BUILD_METADATA_FILE_PATH
      value: ".iib-build-metadata.json"
```

## Related documentation

- [Operator Lifecycle Manager (OLM)](https://olm.operatorframework.io/)
- [File-based catalogs](https://olm.operatorframework.io/docs/concepts/olm-architecture/operator-catalog/creating-a-catalog/#file-based-catalogs)
- [OPM (Operator Package Manager)](https://github.com/operator-framework/operator-registry)
- [Buildah](https://buildah.io/)
- [Repository README](../../README.md) — tests, container image build, and repository layout
