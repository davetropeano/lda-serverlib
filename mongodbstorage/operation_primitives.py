from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from datetime import datetime
from dateutil import tz
from storage_mapping import rdf_json_from_storage
from storage_mapping import query_to_storage
from storage_mapping import storage_value_from_rdf_json
from storage_mapping import predicate_to_mongo
from storage_mapping import fix_up_url_for_storage
import os
import threading
import logging
import utils #TODO this is a cyclical dependency between lib-serverlib and lib-mongodbstorage

def get_timestamp():
    #return datetime.utcnow()
    return datetime.now(tz.tzutc())

MONGO_CLIENT = MongoClient(os.environ['MONGODB_DB_HOST'], int(os.environ['MONGODB_DB_PORT']), tz_aware=True)
MONGO_DB = MONGO_CLIENT[os.environ['APP_NAME']]
if 'MONGODB_DB_USERNAME' in os.environ: 
    MONGO_DB.authenticate(os.environ['MONGODB_DB_USERNAME'], os.environ['MONGODB_DB_PASSWORD'])

next_id = 1
next_history_id = 1
lineage = None
history_lineage = None
inc_lock = threading.Lock()
def get_lineage():
    lineages_collection = MONGO_DB['lineages_collection']
    lineages_collection.ensure_index( 'lineage_value' )
    result = MONGO_DB.command(
        'findAndModify',
        'lineages_collection',
        query  = {'_id': 'lineage_document'}, 
        update = {'$inc': {'lineage_value': 1}}, 
        new    = True, 
        upsert = True,
        full_response = True)
    if not result['ok']: # "No matching object found"
        logging.debug('find_and_modify_command failed to find or create lineage_document - errmsg: %s datetime: %s' % (result['errmsg'],  datetime.now()))
    else:
        lastErrorObject = result['lastErrorObject']
        if lastErrorObject['n'] == 1:
            lineage = result['value']['lineage_value']
            if lastErrorObject['updatedExisting']:
                logging.debug('find_and_modify_command successfully incremented lineage property of existing document. New value is: %d proc_id: %s datetime: %s' % (lineage, os.getpid(),  datetime.now()))
            else:
                logging.debug('find_and_modify_command successfully created new lineage document. Value of lineage property is: %d proc_id: %s datetime: %s' % (lineage, os.getpid(),  datetime.now()))
            return lineage
        else:
            if lastErrorObject['updatedExisting']:
                logging.debug('find_and_modify_command failed to increment lineage property of existing document. Proc_id: %s datetime: %s' % (os.getpid(), datetime.now()))
            else: 
                logging.debug('find_and_modify_command failed to create initial lineage document Proc_id: %s datetime: ' % (os.getpid(),  datetime.now()))
    return -1

#TODO: The following constants are also defined in storage_mapping. Can't we put them in one place and share?
DC = 'http://purl.org/dc/terms/'
CE = 'http://ibm.com/ce/ns#'
XSD = 'http://www.w3.org/2001/XMLSchema#'
CREATOR = DC+'creator'
CREATED = DC+'created'
MODIFICATIONCOUNT = CE+'modificationCount'
LASTMODIFIED = CE+'lastModified'
LASTMODIFIEDBY = CE+'lastModifiedBy'
HISTORY = CE+'history'
ID = CE+'id'

SYSTEM_PROPERTIES = (CREATOR, CREATED, MODIFICATIONCOUNT, LASTMODIFIED, LASTMODIFIEDBY, HISTORY, '@id', '_id')
        
def create_document(user, rdf_json, public_hostname, tenant, namespace, resource_id=None):
    # create storage format and put it in the database.
    # rdf_json is the document to be stored, in rdf_json format
    # The storage format is the following:
    # {  '_id': docId (may be provided by caller in '' subject in json_ld, or a value provided here)
    #    '_modificationCount' : number
    #    '@id': document_url (with domain replaced by "urn:ce:" and periods escaped to %2E)
    #    '@graph' : 
    #       [
    #           {   '@id' : subject_url, (with domain replaced by "urn:ce:" if the url is on the same site and periods escaped to %2E)
    #               <predicate> : {'type' : <rdf-json type>, 'value' : <value>, 'datatype'=<datatype>}
    #               ... repeat ...
    #               },
    #           {
    #               ... repeat for additional subjects ...
    #               }
    #           ]
    #   }
    if resource_id == None:
        resource_id = make_objectid()
    document_url = utils.construct_url(public_hostname, tenant, namespace, resource_id)
    subject_array = make_subject_array(rdf_json, public_hostname, document_url)
    if subject_array is None: return (400, None, 'cannot set system property')
    timestamp = get_timestamp()
    json_ld = {'_id' : resource_id, '@graph': subject_array, '@id' : fix_up_url_for_storage('', public_hostname, document_url)}
    json_ld['_modificationCount'] =  0
    json_ld['_created'] = json_ld['_lastModified'] = timestamp
    json_ld['_createdBy'] = json_ld['_lastModifiedBy'] = fix_up_url_for_storage(user, public_hostname, document_url)
    try:
        MONGO_DB[make_collection_name(tenant, namespace)].insert(json_ld)
    except DuplicateKeyError:
        return (409, document_url, 'duplicate document id: %s' % resource_id)
    return (201, document_url, rdf_json_from_storage(json_ld, public_hostname)) # status_code, headers, body (which could contain error info)

def execute_query(user, query, public_hostname, tenant, namespace, projection=None):
    collection_url = utils.construct_url(public_hostname, tenant, namespace, None)
    query = query_to_storage(query, public_hostname, collection_url)
    #logging.debug(query)
    if projection is None:
        cursor = MONGO_DB[make_collection_name(tenant, namespace)].find(query)
    else:
        # Note: projection must NOT suppress the @id field (@id is needed by the storage format conversion routine)
        cursor = MONGO_DB[make_collection_name(tenant, namespace)].find(query, projection)       
    result = get_query_result(cursor, public_hostname)
    #logging.debug(result)
    return (200, result)

def get_document(user, public_hostname, tenant, namespace, documentId):
    cursor = MONGO_DB[make_collection_name(tenant, namespace)].find({'_id': documentId})
    try: document = cursor.next()
    except StopIteration: document = None
    if document is not None:
        document = rdf_json_from_storage(document, public_hostname)
        return (200, document)
    else:
        return (404, ['404 not found'])

def delete_document(user, public_hostname, tenant, namespace, document_id):
    MONGO_DB[make_collection_name(tenant, namespace)].remove(document_id, True)
    
def drop_collection(user, public_hostname, tenant, namespace):
    MONGO_DB[make_collection_name(tenant, namespace)].drop()
    
def create_history_document(user, public_hostname, tenant, namespace, document_id):
    cursor = MONGO_DB[make_collection_name(tenant, namespace)].find({'_id': document_id})
    try: storage_json = cursor.next()
    except StopIteration: storage_json = None
    if storage_json is not None:
        storage_json['_versionOfId'] = storage_json['_id']
        storage_json['_versionOf'] = storage_json['@id']
        history_objectId = make_historyid()
        storage_json['_id'] = history_objectId
        history_collection_name = make_collection_name(tenant, namespace + '_history')
        history_document_url = utils.construct_url(public_hostname, tenant, namespace + '_history', history_objectId)
        storage_json['@id'] = fix_up_url_for_storage('', public_hostname, history_document_url)
        MONGO_DB[history_collection_name].insert(storage_json)
        return 201, history_document_url
    else:
        return 404, None

def get_prior_versions(user, public_hostname, tenant, namespace, history):
    query = {'@id': {'$in': [fix_up_url_for_storage(version, public_hostname, '/') for version in history]}}
    cursor = MONGO_DB[make_collection_name(tenant, namespace + '_history')].find(query)
    result = get_query_result(cursor, public_hostname)
    #logging.debug(result)
    return (200, result)
        
def patch_document(user, document, public_hostname, tenant, namespace, document_id):
    # note that creating a history document is idempotent and safe (in practice, if not in principle). This means that if there is a failure after creating 
    # the history document, and before the patch operation, the whole thing can be safely re-run. This may result in two identical history documents, where
    # nomally there would be a difference between any two history documents, but this is perfectly harmless. Only the history document whose ID is referenced 
    # in the successful patch operation will ever be looked at, so the other is just wasting a little disk space.
    status, history_document_id = create_history_document(user, public_hostname, tenant, namespace, document_id)
    if status == 201:                        
        mod_count = document[0]
        if mod_count == -1:
            mod_count_criteria = False
        else:
            mod_count_criteria = True
        new_values = document[1]
        document_url = utils.construct_url(public_hostname, tenant, namespace, document_id)
        delete_subject_urls = [ fix_up_url_for_storage(x, public_hostname, document_url) for x in new_values if new_values[x] is None]
        collection_name = make_collection_name(tenant, namespace)
        if len(delete_subject_urls) != 0:
            criteria = {'_id' : document_id}
            patch = {'$inc' : {'_modificationCount' : 1}, '$pull': { '@graph': { '@id': { '$in': delete_subject_urls } } }, '$push': {'_history' : history_document_id} }
            last_err = MONGO_DB[collection_name].update(criteria, patch)
            if last_err['n'] == 1:
                mod_count = mod_count + 1
            else:
                return (409, 'unexpected update count %s' % last_err)        
        for subject_url, subject_node in new_values.iteritems(): # have to patch one subject at a time, unfortunately 
            if subject_node is None: continue
            # first assume the subject is already in the @graph array, and construct a query that will modify it
            criteria = {subject_url : {}}
            criteria = query_to_storage(criteria, public_hostname, document_url)
            criteria['_id'] = document_id
            if mod_count_criteria:
                criteria['_modificationCount'] = mod_count
            subject_sets = {'_lastModified' : get_timestamp(), '_lastModifiedBy': user}
            subject_unsets = {}
            for predicate, value_array in subject_node.iteritems():
                if predicate in SYSTEM_PROPERTIES or predicate == '_id': return (400, 'cannot set system property')
                if isinstance(value_array, (list, tuple)):
                    if len(value_array) > 0:
                        subject_sets['@graph.$.' + predicate_to_mongo(predicate)] = [storage_value_from_rdf_json(value, public_hostname, document_url) for value in value_array]
                    else:
                        subject_unsets['@graph.$.' + predicate_to_mongo(predicate)] = 1
                else:
                    subject_sets['@graph.$.' + predicate_to_mongo(predicate)] = storage_value_from_rdf_json(value_array, public_hostname, document_url)
            patch = {'$inc' : {'_modificationCount' : 1}, '$set' : subject_sets, '$unset' : subject_unsets, '$push': {'_history' : history_document_id}}
            last_err = MONGO_DB[collection_name].update(criteria, patch)
            if last_err['n'] == 1:
                mod_count = mod_count + 1
            else:
                # our assumption that the subject is already in the @graph array must have been wrong. Construct a query that will add the subject
                criteria = {'_id': document_id}
                if mod_count_criteria:
                    criteria['_modificationCount'] = mod_count
                subject_sets = {'_lastModified' :get_timestamp(), '_lastModifiedBy': user}
                new_subject = {'@id': fix_up_url_for_storage(subject_url, public_hostname, document_url)}
                for predicate, value_array in subject_node.iteritems():
                    if predicate in SYSTEM_PROPERTIES or predicate == '_id': return (400, 'cannot set system property')
                    if isinstance(value_array, (list, tuple)):
                        if len(value_array) > 0:
                            new_subject[predicate_to_mongo(predicate)] = [storage_value_from_rdf_json(value, public_hostname, document_url) for value in value_array]                        
                    else:
                        new_subject[predicate_to_mongo(predicate)] = storage_value_from_rdf_json(value_array, public_hostname, document_url)
                patch = {'$inc' : {'_modificationCount' : 1}, '$set' : subject_sets, '$push': {'_history' : history_document_id, '@graph': new_subject}}
                last_err = MONGO_DB[collection_name].update(criteria, patch)
                if last_err['n'] == 1:
                    mod_count = mod_count + 1
                else:
                    return (409, 'unexpected update count %s' % last_err) 
        return (200, None)
    else:
        return (status, None)

def make_objectid():
    global next_id
    global lineage
    inc_lock.acquire()
    if not lineage:
        lineage = str(get_lineage())
    rslt = next_id
    next_id += 1
    inc_lock.release()
    return '.'.join((lineage, str(rslt)))
    
def make_historyid():
    global next_history_id
    global history_lineage
    inc_lock.acquire()
    if not history_lineage:
        history_lineage = str(get_lineage())
    rslt = next_history_id
    next_history_id += 1
    inc_lock.release()
    return '.'.join((history_lineage, str(rslt)))
    
def get_query_result(cursor, public_hostname):
    batchSize = 100
    cursor.batch_size(batchSize)
    response = []
    for _ in range(batchSize): # TODO: how can client GET subsequent batches, if there are more?
        try: document = cursor.next()
        except StopIteration: break
        document = rdf_json_from_storage(document, public_hostname)
        response.append(document)
    return response
            
def make_subject_array(rdf_json, public_hostname, path_url):
    subject_array = []
    for subject, subject_node in rdf_json.iteritems(): 
        json_ld_subject_node = {}
        for predicate, value_array in subject_node.iteritems():
            if subject == rdf_json.graph_url and predicate in SYSTEM_PROPERTIES: 
                return None
            predicate = predicate_to_mongo(predicate)
            value = [storage_value_from_rdf_json(item, public_hostname, path_url) for item in value_array] if isinstance(value_array, (list, tuple)) else storage_value_from_rdf_json(value_array, public_hostname, path_url)
            json_ld_subject_node[predicate] = value
        json_ld_subject_node['@id'] = fix_up_url_for_storage(subject, public_hostname, path_url)
        subject_array.append(json_ld_subject_node)
    return subject_array
        
def make_collection_name(tenant, namespace):
    return tenant + '/' + namespace