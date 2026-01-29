# MySQL-k8s charm scriptlet
# Constraint coverage:
# - Mutual exclusivity between database, mysql, and mysql-root endpoints
# - Optional endpoints

def init():
    juju.observe("validate", on_validate)

def on_validate(event):
    # CONSTRAINT: Mutual exclusivity
    # Only one of database, mysql, or mysql-root can be integrated at a time
    database_relations = event.relations.get('database', [])
    mysql_relations = event.relations.get('mysql', [])
    mysql_root_relations = event.relations.get('mysql-root', [])
    
    integrated_endpoints = 0
    if len(database_relations) > 0:
        integrated_endpoints += 1
    if len(mysql_relations) > 0:
        integrated_endpoints += 1
    if len(mysql_root_relations) > 0:
        integrated_endpoints += 1
    
    if integrated_endpoints > 1:
        event.reject('mutual_exclusion', ['database', 'mysql', 'mysql-root'])
    
    # CONSTRAINT: Limit constraints
    # Each endpoint limited to 1 integration (when not excluded by mutual exclusivity)
    if len(database_relations) > 1:
        event.reject('limit', 'database:1')
    if len(mysql_relations) > 1:
        event.reject('limit', 'mysql:1')
    if len(mysql_root_relations) > 1:
        event.reject('limit', 'mysql-root:1')
