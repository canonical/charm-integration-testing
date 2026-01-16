from dataclasses import dataclass, field


@dataclass
class JujuStub:
    deployed: list = field(default_factory=list)
    configured: list = field(default_factory=list)
    waited_messages: list = field(default_factory=list)
    waited_scaled: list = field(default_factory=list)
    waited_settled: list = field(default_factory=list)
    integrations: list = field(default_factory=list)
    scp_calls: list = field(default_factory=list)
    ssh_calls: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    applications: dict = field(default_factory=dict)
    configured_applications: list = field(default_factory=list)
    unit_ips: dict = field(default_factory=dict)

    def list_applications(self, model: str):
        """Return list of application names in the model"""
        return self.applications.keys()

    def application_charm(self, model: str, application: str):
        """Return the charm name for a given application"""
        return self.applications[application]

    def integration_exists(self, application1: str, endpoint1: str, application2: str, endpoint2: str, model: str):
        """Check if an integration exists between two applications"""
        return (application1, endpoint1, application2, endpoint2) in self.integrations

    def deploy_application(self, model: str, charm: str, application: str):
        """Mock deploying an application (captures call for verification)"""
        self.deployed.append((model, charm, application))

    def configure_application(self, model: str, application: str, values: dict):
        """Mock configuring an application (captures call for verification)"""
        self.configured_applications.append((model, application, values))

    def wait_application_scaled(self, model: str, application: str, timeout):
        """Wait for application to be scaled (captures call for verification)"""
        self.waited_scaled.append((model, application, str(timeout)))

    def wait_application_settled(self, model: str, application: str, timeout):
        """Wait for application to settle (captures call for verification)"""
        self.waited_settled.append((model, application, str(timeout)))

    def wait_for_unit_message(self, model: str, unit: str, message: str, timeout):
        """Wait for a specific message from a unit (captures call for verification)"""
        self.waited_messages.append((model, unit, message, str(timeout)))

    def scp(self, model: str, source: str, destination: str):
        """Mock SCP file transfer (captures call for verification)"""
        self.scp_calls.append((model, source, destination))

    def ssh(self, model: str, target: str, command: str):
        """Mock SSH command execution (captures call for verification)"""
        self.ssh_calls.append((model, target, command))

    def run_action(self, model: str, unit: str, action: str, params: dict):
        """Mock running an action on a unit (captures call for verification)"""
        self.actions.append((model, unit, action, params))

    def unit_ip(self, model: str, unit: str):
        """Return the IP address of a unit"""
        return self.unit_ips[unit]