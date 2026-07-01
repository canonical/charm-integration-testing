# Bundle Builder & Charm Metadata Override Issues

Issues from https://github.com/canonical/charm-integration-testing/issues filtered to
bundle builder failures and charm metadata / override problems.

Legend: `[ ]` = open/not investigated | `[R]` = reproduced | `[F]` = already fixed | `[?]` = needs more info | `[x]` = resolved

---

## Bundle Builder Issues

### ❌ Still Reproducing

- [R] [#701](https://github.com/canonical/charm-integration-testing/issues/701) — **Bundle builder times out** `[bug, bundle-generation]`
  - Reproduced: `mlflow-server latest/stable` and `netbox-k8s 4/stable` both time out (`Solver timed out after 0:01:00`)
- [R] [#691](https://github.com/canonical/charm-integration-testing/issues/691) — **manila-generic: test_build_bundle fails with UncompletableBundleError** `[bug]`
  - Reproduced: `manila-generic latest/edge` → `Cannot fulfill charm endpoints: manila:neutron-plugin, manila-generic:manila-plugin, neutron-openvswitch:neutron-control, ovn-central:ovsdb-server, ovn-chassis:ovsdb`
- [R] [#663](https://github.com/canonical/charm-integration-testing/issues/663) — **Cannot fulfil newly added charm endpoint `lego:ingress` for `edge` risk**
  - Reproduced: `lego 4/edge` → `Cannot fulfill charm endpoints: lego:ingress`
- [R] [#605](https://github.com/canonical/charm-integration-testing/issues/605) — **test_build_bundle fails for k8s rev 1920 (1.32/stable): cannot fulfill aws-integrator:rds-mysql**
  - Reproduced: `k8s 1.32/stable + aws-integrator latest/stable` → `Cannot fulfill charm endpoints: aws-integrator:rds-mysql`
- [R] [#499](https://github.com/canonical/charm-integration-testing/issues/499) — **`juju-jimm-k8s` has no log-proxy endpoint** `[bug, charm-deployment, bundle-generation]`
  - Reproduced (different error): `juju-jimm-k8s 3/stable` → `UnparsableCharmException: override declares requires endpoints not present in charm metadata at channel 3/stable: ['ingress-ssh', 'internal-ingress', 'logging']`
- [R] [#478](https://github.com/canonical/charm-integration-testing/issues/478) — **Jupyter Controller edge exposed gateway-metadata endpoint that bundle builder cannot fulfill**
  - Reproduced: `jupyter-controller latest/edge` → `Cannot fulfill charm endpoints: jupyter-controller:gateway-metadata`
- [R] [#477](https://github.com/canonical/charm-integration-testing/issues/477) — **kubeflow-profiles rev 855 exposes velero_backup_config endpoints that bundle builder cannot fulfill**
  - Reproduced: `kubeflow-profiles 2.0/edge` → `Cannot fulfill charm endpoints: kubeflow-profiles:profiles-backup-config`
- [R] [#434](https://github.com/canonical/charm-integration-testing/issues/434) — **juju-dashboard-k8s rev 69: bundle builder cannot fulfill `dashboard:http` endpoint** `[bug, charm-deployment, bundle-generation]`
  - Reproduced: `juju-dashboard-k8s 0.15/beta` → `Cannot fulfill charm endpoints: juju-dashboard-k8s:dashboard`

### ✅ Already Fixed

- [F] [#581](https://github.com/canonical/charm-integration-testing/issues/581) — **test_build_bundle fails for openstack-integrator: Cannot fulfill charm endpoints (ironic-api, keystone)**
  - `openstack-integrator latest/beta` builds successfully
- [F] [#574](https://github.com/canonical/charm-integration-testing/issues/574) — **test_build_bundle fails for keystone rev 684: cannot fulfill keystone:keystone-middleware**
  - `keystone 2023.1/stable` builds successfully (keystone-middleware marked optional in override)
- [F] [#563](https://github.com/canonical/charm-integration-testing/issues/563) — **test_build_bundle fails for ceph-mon/microceph: cannot fulfill microceph:ceph-nfs**
  - Confirmed fixed with multi-charm spec (`ceph-mon squid/stable` + `microceph squid/stable`): builds successfully; `ceph-nfs` is correctly marked optional in the override.
- [F] [#541](https://github.com/canonical/charm-integration-testing/issues/541) — **`ephemeral-backend` endpoint cannot be satisfied for `nova-compute` machine charm**
  - `nova-compute 2023.1/stable` builds successfully (ephemeral-backend marked optional in override)
- [F] [#539](https://github.com/canonical/charm-integration-testing/issues/539) — **`provide-cmr-mesh` does not exist for `istio-beacon-k8s` in track 1, only track 2**
  - `istio-beacon-k8s 1/stable` builds successfully (override correctly gates provide-cmr-mesh on track 2+)
- [F] [#602](https://github.com/canonical/charm-integration-testing/issues/602) — **`kfp-ui` endpoint not provided in Kfp Ui charm 2.16/edge**
  - Confirmed: override parses cleanly for `kfp-ui 2.16/edge`.

### ❓ Needs More Info / Cannot Reproduce

- [?] [#678](https://github.com/canonical/charm-integration-testing/issues/678) — **`spark-history-server-k8s` channel needs condition on `oauth2-proxy` endpoint**
  - `spark-history-server-k8s 3/stable` and `4/edge` both build successfully. The `oauth2-proxy` endpoint does not appear in current published metadata — may be stale or only in future revisions.
- [?] [#647](https://github.com/canonical/charm-integration-testing/issues/647) — **Bundle builder ignore charms with `listed: false` in overrides** `[enhancement, bundle-generation]`
  - Feature request; not reproducible via a single spec run.
- [?] [#634](https://github.com/canonical/charm-integration-testing/issues/634) — **Bundle builder cannot discover providers available only on non-default tracks** `[bug]`
  - Needs a specific example charm with non-default track providers.
- [?] [#543](https://github.com/canonical/charm-integration-testing/issues/543) — **`memcache` endpoint has no providers for machine charms**
  - Specific affected charm not identified (Test Observer link: charms/326896).
- [?] [#542](https://github.com/canonical/charm-integration-testing/issues/542) — **`nrpe-external-master` endpoint has no requirers**
  - Specific affected charm not identified (Test Observer link: charms/407679).
- [?] [#450](https://github.com/canonical/charm-integration-testing/issues/450) — **mlflow-server deployments fail because no charm currently fulfills the kubernetes_manifest endpoint**
  - `mlflow-server latest/stable` now times out (solver timeout) rather than the original `kubernetes_manifest` failure — a different issue (#701 class) is now masking it.

---

## Charm Metadata / Override Issues

### ❌ Still Reproducing

- [R] [#410](https://github.com/canonical/charm-integration-testing/issues/410) — **discourse-k8s OAuth integration requires force_https config but bundle doesn't set it** `[bug, charm-deployment, bundle-generation]`
  - Reproduced (worse than expected): `discourse-k8s latest/stable` → `Cannot fulfill charm endpoints: discourse-k8s:oauth` — the override is incomplete; `oauth`, `logging`, `nginx-route` are unmarked optional, so the bundle can't even build.
- [R] [#324](https://github.com/canonical/charm-integration-testing/issues/324) — **discourse-k8s charm should have external_hostname property explicitly set** `[bug, charm-deployment, bundle-generation]`
  - Same root cause as #410 — bundle fails to build before config issues are reached.
- [R] [#303](https://github.com/canonical/charm-integration-testing/issues/303) — **netbox-k8s test bundle missing hydra ingress configuration** `[bug, charm-deployment]`
  - `netbox-k8s 4/stable` now hits solver timeout (same #701 class issue).

### ✅ Already Fixed

- [F] [#602](https://github.com/canonical/charm-integration-testing/issues/602) — **`kfp-ui` endpoint not provided in Kfp Ui charm 2.16/edge** (override declares endpoints not in published charm)
  - `kfp-ui 2.16/edge` builds successfully; override criteria updated to cover this channel.
- [F] [#456](https://github.com/canonical/charm-integration-testing/issues/456) — **Invalid config options for `oauth2-proxy-k8s` on channel `0.1/stable`**
  - `oauth2-proxy-k8s 0.1/stable` builds successfully.
- [F] [#599](https://github.com/canonical/charm-integration-testing/issues/599) — **Override needed for `oauth2-proxy-k8s`**
  - Override created: `auth-proxy`/`forward-auth` provides marked optional; constraint enforces `dev: true` when connecting to TLS oauth provider (hydra). Standalone `latest/stable` build succeeds.
  - Note: full `hydra + oauth2-proxy-k8s` bundle still hits a pre-existing solver limitation (can't auto-expand domain on `CHARM_CUSTOM_CONSTRAINT` — hydra needs traefik before its TLS constraints can be checked).
