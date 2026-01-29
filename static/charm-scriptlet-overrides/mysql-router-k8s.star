# MySQL-router-k8s charm scriptlet
# Constraint coverage:
# - Required endpoint (backend-database must be integrated)
# - Limit constraints on requires endpoints

def init():
    juju.observe("validate", on_validate)

def on_validate(event):
    # CONSTRAINT: Required endpoint
    # MySQL Router requires a backend-database connection to function
    backend_relations = event.relations.get('backend-database', [])
    if len(backend_relations) == 0:
        event.reject('required', 'backend-database')
    
    # CONSTRAINT: Limit constraints
    # Each requires endpoint limited to 1 integration
    if len(backend_relations) > 1:
        event.reject('limit', 'backend-database:1')
    
    certificates_relations = event.relations.get('certificates', [])
    if len(certificates_relations) > 1:
        event.reject('limit', 'certificates:1')
    
    logging_relations = event.relations.get('logging', [])
    if len(logging_relations) > 1:
        event.reject('limit', 'logging:1')
    
    tracing_relations = event.relations.get('tracing', [])
    if len(tracing_relations) > 1:
        event.reject('limit', 'tracing:1')
