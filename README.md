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

- `UV_FILE`: Path to a pre-downloaded `uv` binary (will be used when injecting validators, otherwise will be downloaded)
- `UBUNTU_PRO_TOKEN`: Ubuntu Pro token (required for testing canonical-livepatch-server charms)

### Kubernetes test configuration

For tests using Kubernetes clouds, export `KUBECONFIG_<cloud_name>` **before starting
the test process**. The value is a kubeconfig file path, not its YAML contents.
Use the Juju cloud name, replacing hyphens with underscores. For the sandbox's
`local-k8s` cloud:

```bash
export KUBECONFIG_local_k8s=/home/ubuntu/k8s.yaml
```

Configure each Kubernetes cloud used by the run, including target, neighbor and
controller clouds when they differ. Plain `KUBECONFIG`, the default
`~/.kube/config`, and an existing `juju add-k8s` registration do not supply this
mapping. It is needed for direct Kubernetes operations such as controller
restart, resource tracking and Kubernetes log collection, not just chaos tests.
Machine-only runs do not need it.

Without a cloud mapping, tests requiring direct Kubernetes access skip with the
variable to set; this is not restart, pod-deletion or chaos coverage. Tests using
only Juju can still run when the cloud is already registered. An explicit mapping
to a missing/invalid file, authentication or permission errors, and operation
failures remain errors, not skips. Resource tracking and Kubernetes log collection
also need the mapping and may report collection warnings without it.

See the [local Kubernetes configuration guide](docs/how-to/run-and-debug-tests-locally.rst#configure-kubernetes-access-for-the-test-suite)
for multiple-cloud examples and troubleshooting.

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
