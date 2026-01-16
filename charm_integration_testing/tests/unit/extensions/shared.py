from dataclasses import dataclass, field


@dataclass
class JujuStub:
    deployed: list = field(default_factory=list)
    configured: list = field(default_factory=list)
    waited_scaled: list = field(default_factory=list)
    waited_settled: list = field(default_factory=list)
    scp_calls: list = field(default_factory=list)
    ssh_calls: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    applications: dict = field(default_factory=dict)
    unit_ips: dict = field(default_factory=dict)

    def list_applications(self, model: str):
        return self.applications.keys()

    def application_charm(self, model: str, application: str):
        return self.applications[application]

    def deploy_application(self, model: str, charm: str, application: str):
        self.deployed.append((model, charm, application))

    def configure_application(self, model: str, application: str, values: dict):
        self.configured.append((model, application, values))

    def wait_application_scaled(self, model: str, application: str, timeout):
        self.waited_scaled.append((model, application, str(timeout)))

    def wait_application_settled(self, model: str, application: str, timeout):
        self.waited_settled.append((model, application, str(timeout)))

    def scp(self, model: str, source: str, destination: str):
        self.scp_calls.append((model, source, destination))

    def ssh(self, model: str, target: str, command: str):
        self.ssh_calls.append((model, target, command))

    def run_action(self, model: str, unit: str, action: str, params: dict):
        self.actions.append((model, unit, action, params))

    def unit_ip(self, model: str, unit: str):
        return self.unit_ips[unit]