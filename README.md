# Overview

A collection of Pytest-based tests for Charm Integration Testing, focusing on
validating the deployment and interoperability of charms.

## Getting Started

For local charm testing, use the [development sandbox](development-sandbox/README.md).
It mounts your checkout and installs the tools inside a VM.

To install directly on your machine, install mise (on Ubuntu,
`sudo snap install mise --classic`; see
[getting started](https://mise.jdx.dev/getting-started.html) for other
platforms), then prepare the development environment:

```bash
mise run setup -- --group development
```

Setup installs Python, uv and project dependencies without changing system
packages. Common tasks:

```bash
mise run setup -- --group development integration  # Live charm testing tools
mise run format
mise run lint
mise run test:suite -- ...  # Live integration suite
```

Run `mise tasks` to list all tasks and `mise run setup -- --help` for groups.

See [tool ownership](CONTRIBUTING.md#what-goes-where) for locked tools,
machine-wide snap channels, and Python dependencies.

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
