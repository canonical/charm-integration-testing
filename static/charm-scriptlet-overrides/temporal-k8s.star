# Temporal-k8s charm scriptlet
# Constraint coverage:
# - Capability requirements (endpoints must integrate with specific charms)

def init():
    juju.observe("validate", on_validate)

def on_validate(event):
    # CONSTRAINT: Capability requirements
    # The admin endpoint must integrate with temporal-admin-k8s specifically
    # The ui endpoint must integrate with temporal-ui-k8s specifically
    # Both provide the same "temporal" interface, but temporal-k8s needs specific providers
    
    # Check admin endpoint
    admin_relations = event.relations.get('admin', [])
    if admin_relations:
        for app_name in admin_relations:
            charm_name = event.charm_names.get(app_name, '')
            if charm_name != 'temporal-admin-k8s':
                # Format: ['endpoint', 'required_charm1', 'required_charm2', ...]
                event.reject('capability', ['admin', 'temporal-admin-k8s'])
                return
    
    # Check ui endpoint  
    ui_relations = event.relations.get('ui', [])
    if ui_relations:
        for app_name in ui_relations:
            charm_name = event.charm_names.get(app_name, '')
            if charm_name != 'temporal-ui-k8s':
                event.reject('capability', ['ui', 'temporal-ui-k8s'])
                return
