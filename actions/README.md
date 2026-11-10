# Charm QA actions

Composite actions for running Charm QA integration tests from another repository's workflow.
Pin a commit SHA: `canonical/charm-integration-testing/actions/<name>@<sha>`.

| Action | Purpose |
|---|---|
| `install-juju` | Install the Juju client snap (`channel` input) |
| `setup-local-k8s` | Initialise Canonical Kubernetes on the runner, register it with Juju and install Chaos Mesh |
| `setup-local-lxd` | Initialise LXD on the runner for Juju's `localhost` cloud |
| `run-charm-tests` | Run the suite against a charm and write JUnit, bundles and logs |

Inputs and outputs are documented in each `action.yaml`. The actions run the shared Mise tasks
from the downloaded copy of this repository at the pinned SHA; they do not read the caller's Mise
configuration. Substrate setup needs passwordless sudo, and Docker is unusable on the runner after
`setup-local-k8s`. On arm64 runners, `run-charm-tests` uses the runner architecture by default.

```yaml
jobs:
  charm-qa:
    runs-on: ubuntu-24.04
    steps:
      - uses: canonical/charm-integration-testing/actions/install-juju@<sha>
        with:
          channel: 3.6/stable
      - id: cloud
        uses: canonical/charm-integration-testing/actions/setup-local-k8s@<sha>
      - id: tests
        uses: canonical/charm-integration-testing/actions/run-charm-tests@<sha>
        with:
          target-charm: prometheus-k8s
          target-endpoint: grafana-source
          target-cloud: ${{ steps.cloud.outputs.cloud-name }}
          target-kubeconfig: ${{ steps.cloud.outputs.kubeconfig-path }}
          target-platform: ${{ steps.cloud.outputs.platform }}
          neighbor-charm: grafana-k8s
          neighbor-endpoint: grafana-source
      - if: ${{ !cancelled() }}
        uses: actions/upload-artifact@<sha>
        with:
          name: charm-qa
          path: |
            ${{ steps.tests.outputs.junit-path }}
            ${{ steps.tests.outputs.log-dir }}
```

For cross-model tests, also set `neighbor-cloud` and the matching `neighbor-*` inputs, for example
with `setup-local-lxd` for a machine neighbor.
