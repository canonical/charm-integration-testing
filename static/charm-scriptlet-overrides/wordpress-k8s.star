# WordPress-k8s charm scriptlet
# Constraint coverage:
# - Required integration (database)

def init():
    juju.observe("validate", on_validate)

def on_validate(event):
    # CONSTRAINT: Required integration
    # WordPress requires a database provider (database endpoint)
    if 'database' not in event.relations:
        event.reject('required', 'database')
