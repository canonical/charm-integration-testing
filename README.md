# Overview

A collection of Pytest-based tests for Charm Integration Testing, focusing on
validating the deployment and interoperability of charms.

## Getting Started

Install [mise](https://mise.jdx.dev/getting-started.html) first. Tool versions
are pinned with mise, and Python dependencies are managed through Poetry.
The base `mise.toml` contains the test-execution toolchain used locally and in CI.

```bash
mise run setup
```

## Development

Development tools (including linters) are added by `mise.development.toml`.
Select them with `export MISE_ENV=development` before following the installation
commands above. The sandbox adds its own extras from
`development-sandbox/mise.toml` automatically.

See [CONTRIBUTING.md](CONTRIBUTING.md) for more information about development
best practices.

## Usage

```bash
mise run test:suite -- \
    --target-cloud "{juju_cloud}" \
    --target-charm "{target_charm}" \
    --target-endpoint "{target_endpoint}" \
    --neighbor-charm "{neighbor_charm}" \
    --neighbor-endpoint "{neighbor_endpoint}"
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
