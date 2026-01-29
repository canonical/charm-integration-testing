# PGBouncer-k8s charm scriptlet
# Constraint coverage:
# - Required integration: backend database connection (requires endpoint)
# - Conditional requirement: at least one client endpoint must be integrated

def init():
    juju.observe("validate", on_validate)

def on_validate(event):
    # CONSTRAINT: Required integration
    # PGBouncer requires a backend database (postgresql) to proxy to
    if 'backend-database' not in event.relations:
        event.reject('required', 'backend-database')
    
    # CONSTRAINT: Conditional requirement
    # PGBouncer needs at least one client endpoint to exit blocked state
    has_client = ('database' in event.relations or 
                  'db' in event.relations or 
                  'db-admin' in event.relations)
    
    if not has_client:
        event.reject('conditional', ['database', 'db', 'db-admin'])
