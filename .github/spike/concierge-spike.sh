#!/usr/bin/env bash
# Throwaway Concierge spike for SQT-1273. Usage: concierge-spike.sh <k8s|lxd|both|additive>
set -euo pipefail
case_name=$1
summary=${GITHUB_STEP_SUMMARY:-/dev/stdout}
arch=$(dpkg --print-architecture)
declare -A sums=(
    [amd64]=0004338c4ea2d4bebc42311483c2ce1be135c107683d54f13d1da5af77d6af83
    [arm64]=dd97d9bbeaf31685dc4076e250697edc68baa314ebbcb4719a6dbad59d61553a
)

phase() {
    local name=$1
    shift
    local start=$SECONDS rc=0
    echo "::group::$name"
    set +e
    (set -e; "$@")
    rc=$?
    set -e
    [[ $rc == 0 ]] || diagnostics
    echo "::endgroup::"
    echo "| $name | $((SECONDS - start))s | $([[ $rc == 0 ]] && echo ok || echo "FAIL ($rc)") |" >> "$summary"
    return "$rc"
}

diagnostics() {
    echo "--- diagnostics"
    juju status --relations 2>/dev/null || true
    kubectl --kubeconfig "$HOME/k8s.yaml" get pods -A -o wide 2>/dev/null || true
    sudo iptables -S FORWARD | head -5
}

baseline() {
    nproc; free -h; df -h / /mnt 2>/dev/null || df -h /
    systemctl is-active docker containerd || true
    sudo iptables -S FORWARD | head -3
    snap list
}

install_concierge() {
    local tarball=concierge_1.9.0_linux_${arch}.tar.gz
    curl -fsSLO "https://github.com/canonical/concierge/releases/download/v1.9.0/$tarball"
    echo "${sums[$arch]}  $tarball" | sha256sum -c -
    tar -xzf "$tarball" concierge
    sudo install concierge /usr/local/bin/concierge
}

write_config() {
    local file=$1 k8s=$2 lxd=$3
    cat > "$file" <<CFG
juju:
  disable: true
providers:
  k8s:
    enable: $k8s
    bootstrap: false
    channel: 1.32-classic/stable
    features:
      local-storage: {}
      network: {}
      load-balancer:
        l2-mode: "true"
        cidrs: "10.43.45.0/28"
  lxd:
    enable: $lxd
    bootstrap: false
host:
  snaps:
    helm:
      channel: 3/stable
CFG
}

prepare() {
    sudo concierge prepare -c "$1" --verbose
}

install_juju() {
    sudo snap install juju --channel 3/stable
    mkdir -p ~/.local/share/juju
}

register_k8s() {
    sudo k8s config > "$HOME/k8s.yaml"
    KUBECONFIG=$HOME/k8s.yaml juju add-k8s local-k8s --client
    juju clouds --client
    ls -l /run/containerd/containerd.sock
}

chaos_mesh() {
    helm install chaos-mesh chaos-mesh --repo https://charts.chaos-mesh.org \
        --kubeconfig "$HOME/k8s.yaml" --namespace chaos-mesh --create-namespace \
        --version 2.8.4 --set-string chaosDaemon.runtime=containerd \
        --set-string chaosDaemon.socketPath=/run/containerd/containerd.sock \
        --set dashboard.create=false --atomic --timeout 10m
    kubectl --kubeconfig "$HOME/k8s.yaml" -n chaos-mesh get pods
}

deploy_k8s() {
    juju bootstrap local-k8s k8s-ctrl --bootstrap-constraints root-disk=5G
    juju add-model spike
    juju set-model-constraints "arch=$arch"
    juju deploy self-signed-certificates --channel 1/stable
    juju wait-for application self-signed-certificates --query='status=="active"' --timeout 15m
}

deploy_lxd() {
    juju bootstrap localhost lxd-ctrl
    juju add-model spike
    juju set-model-constraints "arch=$arch"
    juju deploy ubuntu --base ubuntu@24.04
    juju wait-for application ubuntu --query='status=="active"' --timeout 15m
}

{
    echo "### $case_name on $arch"
    echo "| phase | duration | result |"
    echo "|---|---|---|"
} >> "$summary"

phase baseline baseline
phase install-concierge install_concierge
case $case_name in
    k8s) write_config cfg.yaml true false; phase prepare prepare cfg.yaml ;;
    k8s-forward-accept)
        write_config cfg.yaml true false; phase prepare prepare cfg.yaml
        # Docker on hosted runners sets FORWARD DROP; Concierge only resets it for LXD.
        phase forward-accept sudo iptables -P FORWARD ACCEPT
        ;;
    lxd) write_config cfg.yaml false true; phase prepare prepare cfg.yaml ;;
    both) write_config cfg.yaml true true; phase prepare prepare cfg.yaml ;;
    additive)
        write_config k8s.yaml true false; phase prepare-k8s prepare k8s.yaml
        write_config lxd.yaml false true; phase prepare-lxd-additive prepare lxd.yaml
        phase prepare-rerun prepare lxd.yaml
        ;;
esac
phase install-juju install_juju
if [[ $case_name != lxd ]]; then
    phase register-k8s register_k8s
    phase chaos-mesh chaos_mesh
    phase deploy-k8s deploy_k8s
fi
if [[ $case_name != k8s* ]]; then
    # Concierge adds the runner user to the lxd group; this shell predates that.
    phase deploy-lxd sg lxd -c "arch=$arch; $(declare -f deploy_lxd); deploy_lxd"
fi
phase after baseline
