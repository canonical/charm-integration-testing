# Charms that cannot build a bundle

Some charms assume the existence of a k8s model hosting some required service. In such a case, where the relation is non-optional and it's multiplatform. I decided to not mark them as optional (as that would only cause deployment to fail), and instead document the reasoning here.

### jenkins

- **Blocked on**: `jenkins:agent` (requires)
- **Reason**: jenkins-agent(machine) integrates with jenkin(k8s)
- **Status**: Requires multiple platforms and CMR

### opentelemetry-collector

- **Blocked on**: `opentelemetry-collector:grafana-dashboards-provider` (provides)
- **Reason**: `opentelemetry-collector` (machine) provides `grafana-dashboards-provider`,
  which is only consumed by `grafana-k8s` (kubernetes). There is no machine-platform
  consumer of this interface on Charmhub, so the endpoint can never be fulfilled within a
  single-platform bundle.
- **Status**: Requires multiple platforms and CMR
