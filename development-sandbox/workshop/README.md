# Charm QA Workshop SDK

This draft SDK packages the QA toolkit and its skills independently of the
project mounted at `/project`. It installs no coding agent or Paseo daemon.
Those can be added as separate SDKs in the same Workshop VM.

Tool versions and snap channels come from the packaged `mise.toml`, `mise.lock`
and `.mise/locks/`; Python dependencies come from `uv.lock`.
The YAML declares only the Ubuntu base and SDK metadata, not tool versions.
The Mise bootstrap follows the existing sandbox's stable snap channel.

## Build and try

On a dedicated Ubuntu host with a configured LXD builder and Workshop's
[prerequisites](https://github.com/canonical/workshop#installation):

```bash
mise run setup -- --packages snap:sdkcraft snap:workshop
# Commit SDK changes first: packaging deliberately ignores uncommitted files.
mise run workshop:pack -- --try HEAD
```

The task exports a committed repository snapshot into a temporary build
directory, so local `.env`, MCP credentials and virtualenvs never enter the
SDKcraft build context. Artifacts go to `dist/workshop/`. The source commit
identifies the SDK version; no moving branch is cloned at launch.
Use `mise run workshop:pack -- REF` to pack without installing in the try area.

Copy this directory's `workshop.yaml` into another project's root, then:

```bash
workshop launch
workshop shell
charm-qa run --list
charm-qa run sandbox:dev-validate -- --help
```

The example selects `try-charm-qa`. This draft does not register or publish
`charm-qa` in the SDK Store. After registration and release with SDKcraft,
consumers would select `name: charm-qa` instead. See the upstream
[publishing guide](https://github.com/canonical/workshop/blob/main/docs/how-to/develop-sdks/publish-an-sdk.rst).

## Toolkit and workspace

`/project` remains the consumer's checkout. The SDK keeps a writable copy of
its own toolkit in a private persistent mount, with a separate Python
environment per source revision. Editable installs never write to `/project`.

```bash
charm-qa root
charm-qa skills
charm-qa run setup -- --group local-k8s
charm-qa exec -- juju controllers
```

`run` executes existing Mise tasks from the toolkit root; workspace inputs
must use absolute paths. `exec` keeps the caller's directory and adds the
installed Mise tools and Python environment. Both export `PROJECT_ROOT` as the
toolkit and `CHARM_QA_WORKSPACE` as the caller's original directory.

Launch prepares the `development`, `integration` and `cloud-k8s` groups.
It installs Juju and kubectl but does not initialize Kubernetes, LXD, controllers
or models. Provisioning is explicit:

```bash
charm-qa run setup -- --group local-k8s
charm-qa run setup-k8s -- --kubeconfig "$HOME/.kube/charm-qa.yaml"
export KUBECONFIG_local_k8s="$HOME/.kube/charm-qa.yaml"
# Or, for machine charms:
charm-qa run setup -- --group local-lxd
charm-qa run setup-lxd
```

These operations require passwordless sudo and change the VM's firewall.
Concierge can also refresh substrate snaps and stop the containerd service.
Never forward the host's LXD socket into the environment.
The example uses `lxd-vm`; local substrate support in a Workshop container
is not claimed. Controllers and models are not isolated by Git worktrees.

## Agents and Paseo

An agent can use the tools through `charm-qa` without a dedicated QA harness.
Configure its supported skill-directory setting using `charm-qa skills`.
Read the installed toolkit's `.github/copilot-instructions.md` and applicable
`.github/instructions/` files before modifying toolkit code.

The legacy `.agents/skills/system.md` describes the Multipass launcher;
do not install it as Workshop's system prompt. Some skills still contain
`/home/ubuntu` examples: use the Workshop user's `$HOME` and the toolkit root
instead. Adapting and exercising each agent's discovery is still required;
printing a skills path does not prove that an agent loaded it.

Paseo can be composed into the same environment later. Its service must launch
providers with the QA environment and configure skills for the chosen agent.
Daemon connectivity, authentication and provider installation belong to that
SDK, not this one. This draft adds no Copilot launcher and does not depend on
the portable Copilot agent task.

## State and refresh

The SDK declares private mounts for its source copies, Python environments,
Mise tools/config/cache and uv cache under `~/.local/share/charm-qa`, Juju
client state under `~/.local/share/juju`, kubeconfig under `~/.kube`, and Juju
channel preferences under `~/.config/charm-integration-testing`.
Do not remount these paths onto host credential directories.

Setup switches the `current` toolkit link only after dependencies install
successfully. Previous revisions remain available; nothing automatically
destroys live infrastructure or deletes prior source copies.
Kubeconfigs outside `~/.kube`, local substrate disks, and other VM state are
not covered by these mounts. Preserve them separately before a refresh;
refreshing a live QA VM is not yet a supported workflow.

## Validation boundary

The initial target is Ubuntu 24.04 on amd64. Other architectures, real
SDKcraft packing, Workshop launch/refresh, endpoint validation and agent
discovery require validation on an isolated host before release.
