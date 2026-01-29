# PostgreSQL-k8s charm scriptlet
# Constraint coverage:
# - Mutual exclusion (database, db, db-admin are mutually exclusive)
# - Limit constraint (max 1 concurrent relation per endpoint)

def init():
    juju.observe("validate", on_validate)

def on_validate(event):
    # CONSTRAINT: Mutual exclusion
    # PostgreSQL provides three mutually exclusive client endpoints
    # Only one of database, db, or db-admin can be integrated at a time
    
    endpoints = ['database', 'db', 'db-admin']
    integrated = [ep for ep in endpoints if ep in event.relations]
    
    if len(integrated) > 1:
        event.reject('mutual_exclusion', endpoints)
    
    # CONSTRAINT: Limit constraint
    # Each endpoint can have at most 1 active relation
    for endpoint in endpoints:
        if endpoint in event.relations:
            relations = event.relations[endpoint]
            if len(relations) > 1:
                event.reject(endpoint, 'limit is 1 concurrent relation per endpoint')
