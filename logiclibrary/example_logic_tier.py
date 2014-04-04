import operation_primitives
import urlparse, urllib
import numbers
import json, rdf_json
from rdf_json import URI
from trsbuilder import TrackedResourceSetBuilder
import utils
import os
from base_constants import RDFS, RDF, BP, XSD, DC, CE, OWL, TRS, AC, AC_R, AC_C, AC_ALL, ADMIN_USER

HISTORY = CE+'history'
CREATION_EVENT = TRS+'Creation'
MODIFICATION_EVENT = TRS+'Modification'
DELETION_EVENT = TRS+'Deletion'

NAMESPACE_MAPPINGS = {
    RDFS : 'rdfs',
    RDF  : 'rdf', 
    BP :   'bp',
    XSD :  'xsd',
    DC :   'dc',
    CE :   'ce',
    OWL :  'owl',
    AC :   'ac' # TODO: consider changing ac:resource-group to ce:resource-group, and then remove this line
    }

CHECK_ACCESS_RIGHTS = os.environ.get('CHECK_ACCESS_RIGHTS') != 'False'
   
class Domain_Logic(object):

    def __init__(self, environ, change_tracking=False):
        self.environ = environ
        self.claims = utils.get_or_create_claims(environ)
        self.user = self.claims['user']
        self.url_components = utils.get_url_components(environ)
        self.tenant, self.namespace, self.document_id, self.extra_path_segments, self.path, self.path_parts, self.request_hostname, self.query_string = self.url_components
        self.change_tracking = change_tracking # TODO: should we provide a way to turn change_tracking on/off dynamically
        if change_tracking:
            self.trs_builders = {}
            
    def recurse(self, function, namespace=None, document_id=None, extra_path_segments=None, query_string=None):
        # Perform a get operation with the same host-name, tenant and name-space, but new document_id, extra_segements and query_string
        # One implementation option would be to make a new instance of Domain_Logic and give it a new environ dict copy. This implementation is slightly cheaper/messier
        original_namespace = self.namespace
        original_document_id = self.document_id
        original_path = self.path
        original_extra_path_segments = self.extra_path_segments
        original_query_string = self.query_string
        original_path_parts = self.path_parts
        try:
            if namespace is not None:
                self.namespace = namespace
            if document_id is not None:
                self.document_id = document_id
            if extra_path_segments:
                self.extra_path_segments = extra_path_segments
            self.path_parts = ['', self.namespace, self.document_id] if self.namespace and self.document_id else ['', self.namespace] if self.namespace else ['']
            if self.extra_path_segments:
                self.path_parts = self.path_parts + self.extra_path_segments
            self.path = '/'.join(self.path_parts)
            self.query_string = query_string    
            status, headers, container = function()
        finally: 
            self.namespace = original_namespace
            self.document_id = original_document_id
            self.path = original_path
            self.extra_path_segments = original_extra_path_segments
            self.query_string = original_query_string
            self.path_parts = original_path_parts
        return status, headers, container

    def recursive_get_document(self, namespace=None, document_id=None, extra_path_segments=None, query_string=None):
        return self.recurse(self.get_document, namespace, document_id, extra_path_segments, query_string)
        
    def create_document(self, document, document_id=None):
        # this method is called when a POST is made that means 'create'
        # document is a Python dictionary that was created from a json (ld_json) string in the request
        # The return value is a triple of (status, headers, body). The values of headers and body depends on the status
        # 201 - Create            => headers is a list of headers to return to the client. It should contain at least a location entry with the URL 
        #                            of the newly-created resource. If no content_type header is given, it will be set to 'application/ld+json'
        #                            body may be an empty list or a dictionary that contains the ld+json representaton of the created object
        # 401 - Unauthorized      => The values of headers and body are ignored
        # 400 - Bad Request       => headers may be an empty list or may optionally include headers to return to the client
        #                            body should be a list of pairs, where the first element of the pair identifies the field in error, or is ''.
        #                            The second element of the pair should start with a number, a space, and an optional string explaining the error 
        # others                  => headers may be an empty list or may optionally include headers to return to the client
        #                            body should be a list of pairs, where the first element of the pair identifies the field in error, or is ''.
        #                            The second element of the pair should start with a number, a space, and an optional string explaining the error 
        status, headers, container = self.recursive_get_document(query_string=self.query_string+'?non-member-properties' if self.query_string else 'non-member-properties')
        if status == 200:
            return self.insert_document(container, document, document_id)
        else:
            return (status, headers, container)
        
    def insert_document(self, container, document, document_id=None):
        if self.user is None:
            return (401, [], None)
        else:
            if CHECK_ACCESS_RIGHTS:
                if not self.permissions(container) & AC_C:
                    return [403, [], 'not authorized']
            document = rdf_json.RDF_JSON_Document(document, '')
            self.complete_document_for_container_insertion(document, container)
            self.complete_document_for_storage_insertion(document)
            self.preprocess_properties_for_storage_insertion(document)
            status, location, result = operation_primitives.create_document(self.user, document, self.request_hostname, self.tenant, self.namespace, document_id)
            if status ==201:
                if self.change_tracking:
                    self.generate_change_event(CREATION_EVENT, location)
                # Todo: fix up self.document_id, self.path, self.path_parts to match location url of new document
                self.complete_result_document(result)
            return (status, [('Location', str(location))], result)

    def put_document(self, document):
        return(405, [], [('', 'PUT not allowed')])
            
    def execute_query(self, query):
        # queries are safe and idempotent. That is, they do not have side-effects, and (weaker and implied by safe) the result of doing them
        # muultiple times is the same as doing them once. In that sense, they are similar to a GET, but done via POST.
        # user is the URL that identifies the user
        # query is a Python dictionary that was created from a json string in the request. The format of the JSON will depend on the database back-end
        # The return value is a triple of (status, headers, body). The values of headers and body depends on the status
        # 200 - OK                => headers is a list of headers to return to the client. If no content_type header is given, it will be set to 
        #                            'application/ld+json'
        #                            body may be an empty list or a dictionary that contains the json representaton of the query result
        # 401 - Unauthorized      => The values of headers and body are ignored
        # 400 - Bad Request       => headers may be an empty list or may optionally include headers to return to the client
        #                            body should be a list of pairs, where the first element of the pair identifies the field in error, or is ''.
        #                            The second element of the pair should start with a number, a space, and an optional string explaining the error 
        # others                  => headers may be an empty list or may optionally include headers to return to the client
        #                            body should be a list of pairs, where the first element of the pair identifies the field in error, or is ''.
        #                            The second element of the pair should start with a number, a space, and an optional string explaining the error 
        if self.user is None:
            return (401, None, None)
        else:
            if not self.namespace or self.document_id: #trailing / or other problem
                return self.bad_path()
            status, result = operation_primitives.execute_query(self.user, query, self.request_hostname, self.tenant, self.namespace)
            return (status, [], result)

    def execute_action(self, body):
        # user is the URL that identifies the user
        # body is a Python dictionary that was created from a json string in the request. The format of the JSON will depend on the action
        # The return value is a triple of (status, headers, body). The values of headers and body depends on the status
        # 200 - OK                => headers is a list of headers to return to the client. If no content_type header is given, it will be set to 
        #                            'application/ld+json'
        #                            body may be an empty list or a dictionary that contains the json representaton of the query result
        # 401 - Unauthorized      => The values of headers and body are ignored
        # 400 - Bad Request       => headers may be an empty list or may optionally include headers to return to the client
        #                            body should be a list of pairs, where the first element of the pair identifies the field in error, or is ''.
        #                            The second element of the pair should start with a number, a space, and an optional string explaining the error 
        # others                  => headers may be an empty list or may optionally include headers to return to the client
        #                            body should be a list of pairs, where the first element of the pair identifies the field in error, or is ''.
        #                            The second element of the pair should start with a number, a space, and an optional string explaining the error 
        if self.user is None:
            return (401, None, None)
        else:
            return (400, [], 'unknown action')

    def permissions(self, document):
        owner = document.getValue(CE+'owner')
        if self.user == str(owner):
            return AC_ALL # owner can do everything
        else:
            resource_group = document.getValue(AC+'resource-group')                        
            if resource_group:
                permissions_url = utils.construct_url(self.request_hostname, self.tenant, 'ac-permissions') + ('?%s&%s' % (urllib.quote(str(resource_group)), urllib.quote(self.user)))
                r = utils.intra_system_get(permissions_url)
                if r.status_code == 200:
                    return int(r.text)
        return 0

    def resource_groups(self):
        resource_group_url = utils.construct_url(self.request_hostname, self.tenant, 'ac-resource-groups') + ('?%s' % urllib.quote(self.user))
        r = utils.intra_system_get(resource_group_url)
        if r.status_code == 200:
            return json.loads(r.text, object_hook=rdf_json.rdf_json_decoder)
        else:
            return []
        
    def get_document(self):
        # user is the URL that identifies the user
        # The return value is a triple of (status, headers, body). The values of headers and body depends on the status
        # 200 - OK                => headers is a list of headers to return to the client. If no content_type header is given, it will be set to 
        #                            'application/ld+json'
        #                            body is a dictionary that contains the json (or ld+json) representaton of the resource
        # 401 - Unauthorized      => The values of headers and body are ignored
        # 400 - Bad Request       => headers may be an empty list or may optionally include headers to return to the client
        #                            body should be a list of pairs, where the first element of the pair identifies the field in error, or is ''.
        #                            The second element of the pair should start with a number, a space, and an optional string explaining the error 
        # others                  => headers may be an empty list or may optionally include headers to return to the client
        #                            body should be a list of pairs, where the first element of the pair identifies the field in error, or is ''.
        #                            The second element of the pair should start with a number, a space, and an optional string explaining the error 
        if not CHECK_ACCESS_RIGHTS and self.user is None:
            return (401, None, None)
        else:
            if self.document_id is None:
                return self.get_collection()
            if not self.namespace: 
                return [404, [], 'no resource with the URL: %s' % self.request_url()]
            status, document = operation_primitives.get_document(self.user, self.request_hostname, self.tenant, self.namespace, self.document_id)
            if status == 200:
                # we found the document, but is the user entitled to see it?
                if CHECK_ACCESS_RIGHTS:
                    if not self.permissions(document) & AC_R:
                        return [403, [], 'not authorized']
                status, document = self.complete_result_document(document)
            return [status, [], document]

    def get_collection(self):
        # This method returns a storage collection as a Basic Profile Container.
        # TODO: Need to support paging for large collections
        if self.user is None:
            return (401, [], [])
        else:
            if not self.namespace: # nope, not a pre-existing container resource either
                return self.bad_path()
        # TODO: What access control specs govern these "built-in" collections? Who can see them? What resource-group are they part of?
        container_url = utils.construct_url(self.request_hostname, self.tenant, self.namespace)
        container_properties = { RDF+'type': URI(BP+'Container'),
                                 BP+'membershipSubject': URI(container_url),
                                 BP+'membershipPredicate': URI(RDFS+'member'),
                                 CE+'owner': URI(ADMIN_USER),
                                 AC+'resource-group': self.default_resource_group() }
        document = rdf_json.RDF_JSON_Document({ container_url : container_properties }, container_url)
        if self.query_string.endswith('non-member-properties'):
            document.default_subject_url = document.graph_url
            document.graph_url = document.graph_url + '?non-member-properties'
            status = 200
        else:
            status, results = operation_primitives.execute_query(self.user, {}, self.request_hostname, self.tenant, self.namespace)
            if status == 200:
                self.add_member_detail(document, results)
                member_values = []
                for result in results:
                    member_values.append(URI(result.graph_url))
                if len(member_values) != 0:
                    container_properties[RDFS+'member'] = member_values
            else:
                return status, [], results
        return status, [], document
                
    def delete_document(self):
        # user is the URL that identifies the user
        # The return value is a triple of (status, headers, body). The values of headers and body depends on the status
        # 204 - No content        => Successful delete. Headers is an optional list of headers to return to the client. 
        # 401 - Unauthorized      => The values of headers and body are ignored
        # 400 - Bad Request       => headers may be an empty list or may optionally include headers to return to the client
        #                            body should be a list of pairs, where the first element of the pair identifies the field in error, or is ''.
        #                            The second element of the pair should start with a number, a space, and an optional string explaining the error 
        # others                  => headers may be an empty list or may optionally include headers to return to the client
        #                            body should be a list of pairs, where the first element of the pair identifies the field in error, or is ''.
        #                            The second element of the pair should start with a number, a space, and an optional string explaining the error 
        if self.user is None:
            return (401, [], [])
        else:
            if self.document_id is None:
                return self.drop_collection()
            if not self.namespace: #trailing / or other problem
                return self.bad_path() 
            operation_primitives.delete_document(self.user, self.request_hostname, self.tenant, self.namespace, self.document_id)
            if self.change_tracking:
                resource_url = utils.construct_url(self.request_hostname, self.tenant, self.namespace, self.document_id)
                self.generate_change_event(DELETION_EVENT, resource_url)
            return (204, [], [])    
        
    def drop_collection(self):
        if self.user is None:
            return (401, [], [])
        else:
            if not self.namespace: # nope, not a pre-existing container resource either
                return self.bad_path()
            operation_primitives.drop_collection(self.user, self.request_hostname, self.tenant, self.namespace)
            operation_primitives.drop_collection(self.user, self.request_hostname, self.tenant, self.namespace + '_history')
            operation_primitives.drop_collection(self.user, self.request_hostname, self.tenant, self.namespace + '_tracking')
            document_namespace = self.tenant + '/' + self.namespace
            if self.change_tracking and document_namespace in self.trs_builders:
                del self.trs_builders[document_namespace]
            return (204, [], [])    
    
    def patch_document(self, request_body):
        # user is the URL that identifies the user
        # request_body is a Python dictionary that was created from a json string in the request. The format of the JSON will depend on the database back-end
        # The return value is a triple of (status, headers, body). The values of headers and body depends on the status
        # 200 - OK                => Successful patch. Headers is an optional list of headers to return to the client. 
        #                            body is a dictionary that may contain the ld+json representaton of the patched resource
        # 401 - Unauthorized      => The values of headers and body are ignored
        # 400 - Bad Request       => headers may be an empty list or may optionally include headers to return to the client
        #                            body should be a list of pairs, where the first element of the pair identifies the field in error, or is ''.
        #                            The second element of the pair should start with a number, a space, and an optional string explaining the error 
        # others                  => headers may be an empty list or may optionally include headers to return to the client
        #                            body should be a list of pairs, where the first element of the pair identifies the field in error, or is ''.
        #                            The second element of the pair should start with a number, a space, and an optional string explaining the error 
        if self.user is None:
            return (401, [], [])
        else:
            if not self.namespace: #trailing / or other problem
                return self.bad_path()
            resource_url = utils.construct_url(self.request_hostname, self.tenant, self.namespace, self.document_id)
            self.preprocess_properties_for_storage_insertion(rdf_json.RDF_JSON_Document(request_body[1], resource_url))
            mod_count = request_body[0]
            if not (isinstance(mod_count, numbers.Number) and mod_count == (mod_count | 0)):
                return (400, [], [])
            status, result = operation_primitives.patch_document(self.user, request_body, self.request_hostname, self.tenant, self.namespace, self.document_id)   
            if self.change_tracking and status == 200:
                self.generate_change_event(MODIFICATION_EVENT, resource_url)
            if(status == 200):
                get_status, headers, new_document = self.get_document()
                if(get_status == 200):
                    return (200, headers, new_document)
                else:
                    return (200, [], 'Patch was successful but getting the document after returned %s' % get_status)                
            else:
                return (status, [], [result])

    def document_url(self):
        return utils.construct_url(self.request_hostname, self.tenant, self.namespace, self.document_id)
        
    def absolute_url(self, relative_url):
        return urlparse.urljoin(self.request_url(), relative_url)

    def request_url(self):
        qs = self.query_string
        if qs:
            return 'http://%s%s?%s' % (self.request_hostname, self.path, qs)
        else:
            return 'http://%s%s' % (self.request_hostname, self.path)
        
    def add_member_detail(self, container, result):
        for rdf_json_document in result:
            # we will include the membership triples, plus any triples in the same documents. This will pick up the triples that describe the members.
            for subject, subject_node in rdf_json_document.iteritems():
                #warning - tricky code. If a membershipSubject is set to the collection, the member documents will contain triples whose subject is the container itself.
                #To avoid infinite loops, we must not call complete_result_document on this subject. To avoid this, we see if the subject is already in the result
                new_subject = subject not in container
                for predicate, value_array in subject_node.iteritems(): 
                    container.add_triples(subject, predicate, value_array)
                if new_subject:
                    self.complete_result_document(rdf_json.RDF_JSON_Document(container.data, subject))
        
    def add_bpc_member_properties(self, container):
        bp_subject = container.getValue(BP+'membershipSubject')
        bp_object = container.getValue(BP+'membershipObject')
        bp_predicate = container.getValue(BP+'membershipPredicate')
        bp_containerSortPredicate = container.getValue(BP+'containerSortPredicates')
        if not bp_predicate: # this container not based on a simple predicate
            return (200, container)
        elif bp_subject:
            if bp_object: raise ValueError('cannot provide both object and subject')
            query = {str(bp_subject) : {str(bp_predicate) : '_any'}}
        elif bp_object: # subject or object may be set, but not both
            if bp_subject: raise ValueError('cannot provide both object and subject')
            if bp_object == '_any':
                query = {'_any': {str(bp_predicate) : '_any'}}
            else:
                query = {'_any': {str(bp_predicate) : bp_object}}
        else:
            raise ValueError('must provide an object or a subject')
        if CHECK_ACCESS_RIGHTS:
            resource_groups = self.resource_groups()
            query['_any2'] = {}
            if len(resource_groups) > 0:
                if len(resource_groups) > 1:
                    resource_group_value = {'$in': resource_groups}
                else:
                    resource_group_value = resource_groups[0]
                query['_any2']['$or'] = [{CE+'owner': URI(self.user)}, {AC+'resource-group': resource_group_value}]
            else:
                query['_any2'][CE+'owner'] = URI(self.user)
        if bp_containerSortPredicate:
            query = {'$query': query, '$orderby' : {bp_containerSortPredicate: 1}}
        status, result = operation_primitives.execute_query(self.user, query, self.request_hostname, self.tenant, self.namespace)
        if status == 200:
            self.add_member_detail(container, result)
            return (200, container)
        else:
            return (status, result)
            
    def complete_result_document(self, document):
        document_url = document.graph_url #self.document_url()
        if self.extra_path_segments == None: # a simple document URL with no extra path segments
            document.add_triples(document_url, CE+'allVersions', URI('/'.join((document_url, 'allVersions'))))
        else: 
            if len(self.extra_path_segments) == 1 and self.extra_path_segments[0] == 'allVersions' and not self.query_string: # client wants history collection
                status, document = self.create_all_versions_container(document)
                return (status, document)
        if URI(BP+'Container') in document.getValues(RDF+'type'):
            if self.query_string.endswith('non-member-properties'):
                document.default_subject_url = document.graph_url
                document.graph_url = document.graph_url + '?non-member-properties'
                status = 200
            else:
                status, document = self.add_bpc_member_properties(document)
        else:
            status = 200
        if document.graph_url != self.request_url():
            return 404, 'no document matching that url: %s , graph_url: %s' % (self.request_url(), document.graph_url)
        else:
            return status, document
                    
    def complete_document_for_container_insertion(self, document, container):
        bp_subject = container.getValue(BP+'membershipSubject')
        bp_object = container.getValue(BP+'membershipObject')
        bp_predicate = container.getValue(BP+'membershipPredicate')
        if not bp_predicate:
            raise ValueError('must provide container predicate: %s' % container)
        elif bp_subject:
            if bp_object: raise ValueError('cannot provide both object and subject: %s' % container)
            # store the membership triple in the new document
            document.add_triples(bp_subject, bp_predicate, URI('')) # last argument is null relative address of resource-to-be
        elif bp_object: # subject or object may be set, but not both
            if bp_subject: raise ValueError('cannot provide both object and subject: %s' % container)
            # store the membership triple in the new document
            document.add_triple('', bp_predicate, bp_object) # first argument is null relative address of resource-to-be
        else:
            raise ValueError('must provide an object or a subject')

    def complete_document_for_storage_insertion(self, document):
        document.setValue(CE+'owner', URI(self.user))
        if document.getValue(AC+'resource-group') is None:
            default_resource_group = self.default_resource_group()
            if default_resource_group:
                document.setValue(AC+'resource-group', default_resource_group)

    def preprocess_properties_for_storage_insertion(self, rdf_json):
        pass
            
    def default_resource_group(self):
        return URI(urlparse.urljoin(self.request_url(), '/'))

    def add_container(self, document, url_template, membership_resource, membership_predicate, member_is_object, container_resource_group=None, container_owner=None, prototypes=None) :
        container_url = url_template.format('')
        new_url = url_template.format('/new')
        if container_resource_group is None:
            container_resource_group = self.default_resource_group()
        document[container_url] = {
                RDF+'type': URI((BP+'Container')),
                (BP+'membershipSubject' if member_is_object else BP+'membershipObject') : URI(membership_resource),
                BP+'membershipPredicate' : URI(membership_predicate),
                BP+'newMemberInstructions' : URI(new_url),
                AC+'resource-group' : container_resource_group
                }
        if container_owner is not None:
            document[container_url][CE+'owner'] = container_owner
        self.add_new_member_instructions (document, url_template, membership_resource, membership_predicate, member_is_object, prototypes)

    def add_new_member_instructions (self, document, url_template, membership_resource, membership_predicate, member_is_object, prototypes=None) :
        container_url = url_template.format('')
        new_url = url_template.format('/new')
        if container_url == self.request_url():
            document.graph_url = container_url
        elif new_url == self.request_url():
            document.graph_url = new_url
        document[new_url] = { 
                RDF+'type': URI((BP+'NewMemberInstructions')),
                BP+'newMemberContainer' : URI(container_url),
                } 
        if prototypes is not None:
            proto_fragment_index = 0
            prototype_graphs = []
            for label, prototype_url in prototypes.iteritems():
                abs_prototype_url = urlparse.urljoin(container_url, prototype_url)
                fragment_resource = {
                    RDFS+'label' : URI(label),
                    BP+'newMemberPrototype' : URI(abs_prototype_url)
                    }
                proto_id = '/prototype-%d' % proto_fragment_index
                proto_url = url_template.format(proto_id)
                if proto_url == self.request_url():
                    document.graph_url = proto_url
                proto_fragment_index += 1
                document[proto_url] = fragment_resource
                prototype_graphs.append(URI(proto_url))  
            document[new_url][BP+'newMemberPrototypes'] = prototype_graphs
        return container_url

    def create_container(self, url_template, membership_resource, membership_predicate, member_is_object, prototypes=None):
        container_url = url_template.format('')
        document = rdf_json.RDF_JSON_Document ({}, container_url)
        self.add_container(document, url_template, membership_resource, membership_predicate, member_is_object, None, None, prototypes)
        return document 
        
    def container_from_membership_resource_in_query_string(self, url_template, membership_predicate, member_is_object, prototypes=None):
        if self.query_string.endswith('?non-member-properties'):
            qs = self.query_string[:-22]
        else:
            qs = self.query_string
        membership_resource = self.absolute_url(urllib.unquote(qs))
        document = self.create_container(url_template, membership_resource, membership_predicate, member_is_object, prototypes)
        status, document = self.complete_result_document(document)
        return [status, [], document] 

    def create_resource(self, membership_resource, membership_predicate, member_is_object):
        container_url = self.request_url()
        document = rdf_json.RDF_JSON_Document ({}, container_url)
        status, document = self.add_resource_triples(document, membership_resource, membership_predicate, member_is_object)
        return status, document 
        
    def resource_from_object_in_query_string(self, membership_predicate, member_is_object):
        membership_resource = self.absolute_url(urllib.unquote(self.query_string))
        status, document = self.create_resource(membership_resource, membership_predicate, member_is_object)
        headers = []
        if status == 200:
            content_location = document.getValue(membership_predicate, None, membership_resource) if member_is_object else document.getSubject(membership_predicate, None, membership_resource) 
            document.add_triples(self.request_url(), OWL+'sameAs', content_location)
            document.graph_url = str(content_location)
            if status == 200:
                headers.append(('Content-Location', str(content_location)))
        return [status, headers, document] 
        
    def add_resource_triples(self, document, membership_resource, membership_predicate, member_is_object):
        if member_is_object:
            query = {membership_resource : {membership_predicate : '_any'}}
        else: 
            query = {'_any': {membership_predicate : URI(membership_resource)}}
        status, result = operation_primitives.execute_query(self.user, query, self.request_hostname, self.tenant, self.namespace)
        if status == 200:
            if len(result) == 0:
                return (404, [('', '404 error - no such virtual document %s' % result)])
            elif len(result) == 1:
                # we will include the membership triples, plus any triples in the same documents. This will pick up the triples that describe the members.
                self.add_member_detail(document, result)
                return (200, document)
            else:
                return (404, [('', '404 error - ambiguous virtual document - should be a BPC collection?')])
        else:
            return (status, result)
      
    def add_owned_container(self, document, container_predicate, container_path_segment, membership_predicate, foreign_key_is_reversed=False):
        document_url = document.graph_url    
        document.add_triples(document_url, container_predicate, URI(document_url + '/' + container_path_segment))
        if self.request_url().startswith(document_url) and self.extra_path_segments != None and len(self.extra_path_segments) == 1 and self.extra_path_segments[0] == container_path_segment: 
            # client doesn't really want the document, just its owned container
            container_resouce_group = document.getValue(AC+'resource-group')
            container_owner = document.getValue(CE+'owner')
            document.graph_url = document_url + '/' + container_path_segment
            template = '%s{0}' % document.graph_url
            self.add_container(document, template, document_url, membership_predicate, foreign_key_is_reversed, container_resouce_group, container_owner)  
                
    def create_all_versions_container(self, document):
        history = document.getValues(HISTORY)
        status, query_result = operation_primitives.get_prior_versions(self.user, self.request_hostname, self.namespace, history)
        if status == 200:
            request_url = self.request_url() # the url of the allVersions collection
            result_document = rdf_json.RDF_JSON_Document ({}, request_url)
            result_document[request_url] = {
                '#id' : URI('all versions'),
                RDF+'type': URI(BP+'Container'),
                BP+'membershipObject' : URI(document.graph_url),
                BP+'membershipPredicate' : URI(CE+'versionOf')
                }
            result_document.add_triples(document.graph_url, CE+'versionOf', URI(document.graph_url))
            result_document.add_triples(document.graph_url, CE+'graph', [{'type': 'graph', 'value': document}])
            for version in query_result:
                result_document.add_triples(version.graph_url, CE+'versionOf', URI(document.graph_url))
                result_document.add_triples(version.graph_url, CE+'graph', [{'type': 'graph', 'value': version}])
                document_url = version.getValue(CE+'versionOf')
                del version[version.graph_url]
                version.default_subject_url = document_url
            return (200, result_document)
        else:
            return (status, query_result)

    def generate_change_event(self, event_type, resource_uri):
        document_namespace = self.tenant + '/' + self.namespace #Todo: - do this better
        if document_namespace not in self.trs_builders:
            self.trs_builders[document_namespace] = TrackedResourceSetBuilder(self.request_hostname, document_namespace)
        self.trs_builders[document_namespace].addChangeEntry(resource_uri, event_type)

    def namespace_mappings(self):
        return NAMESPACE_MAPPINGS
          
    def convert_rdf_json_to_compact_json(self, document):
        converter = rdf_json.RDF_json_to_compact_json_converter(self.namespace_mappings())
        compact_json = converter.convert_to_compact_json(document)
        rdftype = compact_json['rdf_type']
        if rdftype == BP+'Container':
            members = []
            for member in document.get_container_members():
                members.append(converter.compact_json_object(member, document, []))
            compact_json['bp_members'] = members
        return compact_json

    def bad_path(self):
        return (400, [], [('', '4001 - bad path: %s (trailing / or path too short or other problem)' % self.path)])
        
    def check_input_value(self, rdf_document, predicate, field_errors, type=None):
        value = rdf_document.getValue(predicate)
        if not value: 
            field_errors.append((predicate, 'must provide value'))
            return False
        elif type and not isinstance(value, type):
            field_errors.append((predicate, 'must be a %s' % type)) 
            return False
        return True
    
def get_header(header, headers, default=None):
    headerl = header.lower()
    for item in headers:
        if item[0].lower() == headerl:
            return item[1]
    return default