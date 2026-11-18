# Charm QA Workshop SDK

The Charm QA SDK prepares a Workshop for testing and investigating Juju charms.
It packages this toolkit and its agent skills separately from the project at
`/project`. It installs no agent, controller or local substrate.

Mise owns tool versions and snap channels; uv owns Python dependencies.
SDK hooks handle only Workshop bootstrap, writable source and persistent mounts,
then call the repository's shared Mise setup tasks.

## Build and try

On a dedicated Ubuntu host with a configured LXD builder and Workshop's
[prerequisites](https://github.com/canonical/workshop#installation):

```bash
mise run setup -- --group workshop
# Packaging uses committed files only.
mise run workshop:pack -- --try HEAD
```

The `workshop` group installs LXD, Workshop and SDKcraft. It can refresh an
existing LXD snap to the catalogue channel; it does not initialize LXD.
Do not run it on a shared host without approval.

Packaging exports a committed snapshot into a temporary build directory.
Local credentials, virtualenvs and untracked files are excluded. Artifacts
go to `dist/workshop/`; the source commit identifies the SDK version.
Use `mise run workshop:pack -- REF` to pack without installing in the try area.

Copy `workshop.yaml` into a separate project's `.workshop/charm-qa.yaml`:

```bash
workshop launch
workshop shell
```

Run agents and QA commands **inside** that environment. The example uses
`lxd-container` and composes Canonical's published
[Copilot SDK](https://github.com/canonical/copilot-sdk).
Authenticate using that SDK's documented mechanism, then run `copilot` from
the project directory. Remove Copilot from the definition for tools-only usage
or compose another agent SDK. The example's `agent` action does not use `--yolo`.

The SDK is not published to the Store. After registration and release,
consumers would select `charm-qa` instead of `try-charm-qa`.

## One toolkit, different workspaces

`CHARM_QA_ROOT` identifies the installed toolkit. Login sessions, Workshop
commands and actions receive it along with the SDK's isolated Mise and Python
environment. `/project` remains the consumer's checkout:

```bash
workspace="$PWD"
mise -C "$CHARM_QA_ROOT" tasks ls
mise -C "$CHARM_QA_ROOT" run test:suite -- --help
mise -C "$CHARM_QA_ROOT" exec -- juju clouds --client
```

Tasks execute from the toolkit. Use absolute paths for charm inputs and outputs,
for example `--log-dir "$workspace/test-logs"`. For Python commands, use
`mise -C "$CHARM_QA_ROOT" exec -- uv run --no-sync --all-packages ...`.
No separate QA CLI or global Mise configuration is required.

Framework development uses a writable checkout as the toolkit and workspace.
Charm development uses the same tasks against an installed toolkit and a
separate workspace. Selecting `main`, a development commit or a future release
tag changes the toolkit source, not this interface. The SDK packages its build
commit; launch does not clone a moving branch. It does not automatically select
`/project` as the toolkit when that project happens to be this repository.

## Setup and agents

Launch calls `mise run setup -- --group development integration cloud-k8s skills`.
Skill registration is owned by the shared `setup-skills` task, not the SDK.
See [agent setup](../CONTRIBUTING.md#agent-tools-and-skills) for using it on a
prepared host, in a sandbox or on a runner.

The task registers symlinks in the environment user's `~/.agents/skills` and
`~/.claude/skills`. It rejects conflicting content, leaves unrelated skills
alone and updates only its own links. Registering another checkout explicitly
selects that checkout's skills for this user. `system.md` is not registered.

Agent installation is independent: compose an agent SDK, or explicitly run
`mise -C "$CHARM_QA_ROOT" run setup -- --group agent skills` inside a prepared
environment to install the repository's Copilot tool and skills.
The existing `mise run agent` launcher is for trusted framework development,
supplies framework-specific context and uses `--yolo`; it is not the default.

These registrations are inside Workshop, not on the external host. Do not mount
the host's agent settings or skill directories into it. Additional user skills
need a persistent mount, such as the separate
[Skills SDK](https://github.com/raineszm/skills-sdk); this SDK does not own those
shared directories. The two test-running/setup skills use the common toolkit
interface. Other skills still need a portability audit; registration alone
does not prove a workflow works.

## Substrates and state

Use explicitly provided clouds and credentials. Local Kubernetes and LXD
provisioning inside the container has not been validated. Do not follow
VM-specific provisioning instructions there or forward the host's LXD socket.
Substrate tasks change firewalls and system services; run them only in a
supported environment with authorization.

Persistent mounts cover source copies, Python environments, Mise/uv data,
Juju state, `~/.kube`, Juju channel preferences and the skill-registration source.
Do not mount host credential directories onto these paths. User skill directories
are not mounted by this SDK.

Setup switches the active toolkit after dependency installation succeeds.
Previous source revisions remain; no live infrastructure is automatically
destroyed. Kubeconfigs outside `~/.kube` and local substrate disks need separate
preservation. Refreshing a live QA deployment is not a supported workflow.
