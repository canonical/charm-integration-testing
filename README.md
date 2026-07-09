# Overview

A collection of Pytest-based tests for Charm Integration Testing, focusing on
validating the deployment and interoperability of charms.

## Getting Started

Python dependencies are managed through poetry.

```bash
pipx install poetry==2.0
poetry install
```

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) for more information about development
best practices.

## Usage

```bash
./scripts/run-test-suite.sh \ 
    --model {juju_model} \
    --requirer {application}:{endpoint} \
    --provider {application}:{endpoint}
```

### Optional Environment Variables

The following environment variables can be set for specific test scenarios:

- `MINIO_CLIENT_FILE`: Path to MinIO client (will be used when deploying minio with s3-integrator, otherwise will be downloaded)
- `MINIO_SERVER_FILE`: Path to MinIO server binary (will be used when deploying minio on machine models with s3-integrator, otherwise will be downloaded)
- `UV_FILE`: Path to a pre-downloaded `uv` binary (will be used when injecting validators, otherwise will be downloaded)
- `UBUNTU_PRO_TOKEN`: Ubuntu Pro token (required for testing canonical-livepatch-server charms)
- `KUBECONFIG_<cloud_name>`: Path to kubeconfig for a Kubernetes cloud registered in Juju. One variable per K8s cloud, where hyphens in the cloud name are replaced with underscores (e.g. `KUBECONFIG_microk8s=/home/user/.kube/microk8s.yaml`, `KUBECONFIG_local_k8s=/home/user/.kube/local-k8s.yaml`). Replaces the former single `KUBECONFIG` variable.

## Documentation

Run the documentation locally from the `docs` directory:

```bash
make run
```

For the current local test execution flow (including scheduler states and
Juju options like `--juju-cloud`, `--juju-controller`, and
`--juju-model-config`), see
[`docs/how-to/run-and-debug-tests-locally.rst`](docs/how-to/run-and-debug-tests-locally.rst).

See [CONTRIBUTING.md](CONTRIBUTING.md) for more information about the
documentation setup.
