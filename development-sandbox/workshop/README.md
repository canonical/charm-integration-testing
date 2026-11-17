# Charm QA Workshop SDK

The Charm QA SDK prepares a Workshop for testing and investigating Juju charms.
It packages the QA toolkit and reusable agent skills independently of the
project mounted at `/project`. It installs no coding agent or Paseo daemon.
Those can be added as separate SDKs in the same Workshop.

Tool versions and snap channels come from the packaged `mise.toml`, `mise.lock`
and `.mise/locks/`; Python dependencies come from `uv.lock`.
The YAML declares only the Ubuntu base and SDK metadata, not tool versions.
The Mise bootstrap follows the existing sandbox's stable snap channel.

## Build and try

On a dedicated Ubuntu host with a configured LXD builder and Workshop's
[prerequisites](https://github.com/canonical/workshop#installation):

```bash
mise run setup -- --group workshop
# Commit SDK changes first: packaging deliberately ignores uncommitted files.
mise run workshop:pack -- --try HEAD
```

The `workshop` group installs LXD, Workshop and SDKcraft from the mise
catalogue. It does not initialize LXD or launch a Workshop environment.
It can refresh an existing LXD snap to the catalogue channel.
The example uses `lxd-container`, including when the host is already a Multipass
VM. Do not enable nested Workshop VMs or change shared LXD channels.

The task exports a committed repository snapshot into a temporary build
directory, so local `.env`, MCP credentials and virtualenvs never enter the
SDKcraft build context. Artifacts go to `dist/workshop/`. The source commit
identifies the SDK version; no moving branch is cloned at launch.
Use `mise run workshop:pack -- REF` to pack without installing in the try area.

Copy this directory's `workshop.yaml` into another project's `.workshop`
directory as `charm-qa.yaml`, then:

```bash
workshop launch
workshop run -- tasks
workshop run -- agent
```

The example combines `try-charm-qa` with Canonical's published
[Copilot SDK](https://github.com/canonical/copilot-sdk), `copilot` on
`latest/stable`. Authenticate using that SDK's documented login or secret plug.
The `agent` action starts ordinary interactive Copilot, without `--yolo`.
Remove the Copilot SDK and action for tools-only usage, or replace them with
another agent SDK.

This draft does not register or publish
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
charm-qa tasks
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

These operations require passwordless sudo and change the environment's firewall.
Concierge can also refresh substrate snaps and stop the containerd service.
Never forward the host's LXD socket into the environment.
Local substrate support in a Workshop container is not claimed. Do not run
these provisioning commands there without separately validating that substrate
and approving its host-level changes. Use explicitly configured test clouds
instead. Controllers and models are not isolated by Git worktrees.

## Agents and Paseo

An agent can use the tools through `charm-qa` without a dedicated QA harness.
Following the existing
[Inference Snap Maker SDK](https://github.com/canonical/inference-snap-maker-sdk/blob/main/hooks/setup-project),
`setup-project` registers skills in the Workshop user's home. It creates
individual symlinks in `~/.agents/skills` (Copilot, OpenCode, Oh My Pi) and
`~/.claude/skills` (Claude Code), pointing at the active toolkit's skills.
No client-specific launcher, plugin installation, or global agent settings
change is required. `charm-qa skills` still exposes the source directory.

Existing files, directories, or foreign symlinks with the same skill name cause
setup to fail with the conflicting path, without switching the active toolkit.
Move the conflicting skill aside explicitly before retrying. Setup leaves
unrelated skills untouched, updates owned links through the active toolkit, and
removes only owned links for skills no longer shipped. `system.md` is not a
skill and is never registered.

Registrations are recreated on refresh. This SDK does not mount either shared
skill directory, so it can coexist with agent SDKs and the
[Skills SDK](https://github.com/raineszm/skills-sdk) that own those mounts.
Use that SDK to persist additional user-installed skills under `~/.agents/skills`;
otherwise files outside persistent mounts are discarded by Workshop refresh.

These registrations are inside the Workshop, not the host's default skill
directories. Do not remount the host's agent configuration or skills directories
into it if you want to keep those registrations isolated.

Read the installed toolkit's `.github/copilot-instructions.md` and applicable
`.github/instructions/` files before modifying toolkit code.

`run-charm-tests` and `setup-charm-tests` include Workshop-specific instructions
for the two-checkout layout and explicit cloud configuration. The rest of the
catalogue still needs a portability audit; registration alone does not establish
that a workflow works. In particular, do not follow `/home/ubuntu` paths or
Multipass provisioning instructions in a Workshop container.

Paseo can be composed into the same environment. Its daemon can find the
`charm-qa` command on its PATH; supported providers can discover the same
registered skills. Daemon connectivity, authentication and provider installation
belong to that SDK, not this one.

The optional `mise run agent` development launcher is separate work in
[#1284 (Add a portable development agent task)](https://github.com/canonical/charm-integration-testing/pull/1284).
It is not included in this branch or required by the SDK. It supplies
framework-development context, not a generic charm-project prompt, and currently
uses `--yolo`. Keep it for trusted framework development rather than making it
the default Workshop agent action.

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

SDKcraft packing, container launch/refresh, and Paseo co-installation have been
exercised on Ubuntu 24.04 amd64. An isolated validation build including
uncommitted SDK changes was composed with the published Copilot 1.0.112 SDK:
ordinary `copilot skill list --json` discovered the QA skills from a separate
project, and `charm-qa run test:suite -- --help` succeeded. The project remained
unchanged. A subsequent composition with the Skills SDK retained an unrelated
skill installed with `skills add` across a real SDK refresh, while Copilot still
discovered the QA skills.

These checks do not establish an authenticated agent workflow, live charm
testing, endpoint health, or other clients' discovery. Those and other
architectures require validation before release. Nested Workshop VM validation
is explicitly skipped when running inside Multipass.
