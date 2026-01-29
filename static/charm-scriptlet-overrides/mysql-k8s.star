# MySQL-k8s charm scriptlet
# Constraint coverage:
# - Limit constraint (database endpoint accepts limited relations)

def init():
    juju.observe("validate", on_validate)

def on_validate(event):
    # CONSTRAINT: Limit constraint
    # MySQL database endpoint can have multiple clients, but enforce a reasonable limit
    relations = event.relations.get('database', [])
    # Example: limit to 5 concurrent client relations
    if len(relations) > 5:
        event.reject('limit', 'database:5')
