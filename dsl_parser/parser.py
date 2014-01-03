########
# Copyright (c) 2013 GigaSpaces Technologies Ltd. All rights reserved
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
#    * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    * See the License for the specific language governing permissions and
#    * limitations under the License.
BLUEPRINT = 'blueprint'
IMPORTS = 'imports'
TYPES = 'types'
PLUGINS = 'plugins'
INTERFACES = 'interfaces'
SOURCE_INTERFACES = 'source_interfaces'
TARGET_INTERFACES = 'target_interfaces'
WORKFLOWS = 'workflows'
POLICIES = 'policies'
RELATIONSHIPS = 'relationships'
PROPERTIES = 'properties'

HOST_TYPE = 'cloudify.types.host'
CONTAINED_IN_REL_TYPE = 'cloudify.relationships.contained_in'
PLUGIN_INSTALLER_PLUGIN = 'cloudify.plugins.plugin_installer'
KV_STORE_PLUGIN = 'cloudify.plugins.kv_store'

PLUGINS_TO_INSTALL_EXCLUDE_LIST = {PLUGIN_INSTALLER_PLUGIN, KV_STORE_PLUGIN}

__author__ = 'ran'

import os
import yaml
import copy
import contextlib
from schemas import DSL_SCHEMA, IMPORTS_SCHEMA
from jsonschema import validate, ValidationError
from yaml.parser import ParserError
from urllib import pathname2url
from urllib2 import urlopen, URLError
from collections import OrderedDict

def parse_from_path(dsl_file_path, alias_mapping_dict=None, alias_mapping_url=None, resources_base_url=None):
    with open(dsl_file_path, 'r') as f:
        dsl_string = f.read()
    return _parse(dsl_string, alias_mapping_dict, alias_mapping_url, resources_base_url, dsl_file_path)


def parse_from_url(dsl_url, alias_mapping_dict=None, alias_mapping_url=None, resources_base_url=None):
    with contextlib.closing(urlopen(dsl_url)) as f:
        dsl_string = f.read()
    return _parse(dsl_string, alias_mapping_dict, alias_mapping_url, resources_base_url, dsl_url)


def parse(dsl_string, alias_mapping_dict=None, alias_mapping_url=None, resources_base_url=None):
    return _parse(dsl_string, alias_mapping_dict, alias_mapping_url, resources_base_url)


def _get_alias_mapping(alias_mapping_dict, alias_mapping_url):
    alias_mapping = {}
    if alias_mapping_url is not None:
        with contextlib.closing(urlopen(alias_mapping_url)) as f:
            alias_mapping_string = f.read()
        alias_mapping = dict(alias_mapping.items() + _load_yaml(alias_mapping_string, 'Failed to parse alias-mapping')
                             .items())
    if alias_mapping_dict is not None:
        alias_mapping = dict(alias_mapping.items() + alias_mapping_dict.items())
    return alias_mapping


def _dsl_location_to_url(dsl_location, alias_mapping, resources_base_url):
    dsl_location = _apply_alias_mapping_if_available(dsl_location, alias_mapping)
    if dsl_location is not None:
        dsl_location = _get_resource_location(dsl_location, resources_base_url)
        if dsl_location is None:
            ex = DSLParsingLogicException(30, 'Failed on converting dsl location to url - no suitable location found '
                                              'for dsl {0}'.
            format(dsl_location))
            ex.failed_import = dsl_location
            raise ex
    return dsl_location


def _load_yaml(yaml_stream, error_message):
    try:
        parsed_dsl = yaml.safe_load(yaml_stream)
    except ParserError, ex:
        raise DSLParsingFormatException(-1, '{0}: Illegal yaml; {1}'.format(error_message, ex))
    if parsed_dsl is None:
        raise DSLParsingFormatException(0, '{0}: Empty yaml'.format(error_message))
    return parsed_dsl


def _parse(dsl_string, alias_mapping_dict, alias_mapping_url, resources_base_url, dsl_location=None):
    alias_mapping = _get_alias_mapping(alias_mapping_dict, alias_mapping_url)

    parsed_dsl = _load_yaml(dsl_string, 'Failed to parse DSL')

    if dsl_location:
        dsl_location = _dsl_location_to_url(dsl_location, alias_mapping, resources_base_url)
    combined_parsed_dsl = _combine_imports(parsed_dsl, alias_mapping, dsl_location, resources_base_url)

    _validate_dsl_schema(combined_parsed_dsl)

    blueprint = combined_parsed_dsl[BLUEPRINT]
    app_name = blueprint['name']

    nodes = blueprint['topology']
    _validate_no_duplicate_nodes(nodes)

    top_level_relationships = _process_relationships(combined_parsed_dsl)

    top_level_policies_and_rules_tuple = _process_policies(combined_parsed_dsl[POLICIES]) if \
        POLICIES in combined_parsed_dsl else ({}, {})

    types_descendants = _build_types_descendants(_get_dict_prop(combined_parsed_dsl, TYPES))

    node_names_set = {node['name'] for node in nodes}
    processed_nodes = map(lambda node: _process_node(node, combined_parsed_dsl, top_level_policies_and_rules_tuple,
                                                     top_level_relationships, node_names_set, types_descendants,
                                                     alias_mapping), nodes)
    _post_process_nodes(processed_nodes, _get_dict_prop(combined_parsed_dsl, TYPES),
                        _get_dict_prop(combined_parsed_dsl, RELATIONSHIPS), _get_dict_prop(combined_parsed_dsl,
                                                                                           PLUGINS))

    top_level_workflows = _process_workflows(combined_parsed_dsl[WORKFLOWS]) if WORKFLOWS in \
                                             combined_parsed_dsl else {}

    response_policies_section = _create_response_policies_section(processed_nodes)

    plan = {
        'name': app_name,
        'nodes': processed_nodes,
        RELATIONSHIPS: top_level_relationships,
        WORKFLOWS: top_level_workflows,
        POLICIES: response_policies_section,
        'policies_events': top_level_policies_and_rules_tuple[0],
        'rules': top_level_policies_and_rules_tuple[1]
    }

    return plan


def _build_types_descendants(types):
    types_descendants = {dsl_type_name: [] for dsl_type_name in types.iterkeys()}
    for type_name, type_content in types.iteritems():

        def _add_descendants_if_exists(property_name, type_content, type_name):
            if property_name in type_content:
                descendants = types_descendants.get(type_content[property_name], [])
                descendants.append(type_name)
                types_descendants[type_content[property_name]] = descendants

        _add_descendants_if_exists('derived_from', type_content, type_name)
        _add_descendants_if_exists('implements', type_content, type_name)

    return types_descendants


def _post_process_nodes(processed_nodes, types, relationships, plugins):
    node_name_to_node = {node['id']: node for node in processed_nodes}

    for node in processed_nodes:
        _post_process_node_relationships(node, node_name_to_node, plugins)

    #set host_id property to all relevant nodes
    host_types = _build_family_descendants_set(types, HOST_TYPE)
    contained_in_rel_types = _build_family_descendants_set(relationships, CONTAINED_IN_REL_TYPE)
    for node in processed_nodes:
        host_id = _extract_node_host_id(node, node_name_to_node, host_types, contained_in_rel_types)
        if host_id:
            node['host_id'] = host_id

    #set plugins_to_install property
    for node in processed_nodes:
        if node['type'] in host_types:
            plugins_to_install = {}
            for another_node in processed_nodes:
                #going over all other nodes, to accumulate plugins from different nodes whose host is the current node
                if 'host_id' in another_node and another_node['host_id'] == node['id'] and PLUGINS in another_node:
                    #ok to override here since we assume it is the same plugin
                    for plugin_name, plugin_obj in another_node[PLUGINS].iteritems():
                        #only wish to add agent plugins, and only if they're not the installer plugin
                        if plugin_obj['agent_plugin'] == 'true' and plugin_obj['name'] not in PLUGINS_TO_INSTALL_EXCLUDE_LIST:
                            plugins_to_install[plugin_name] = plugin_obj
            node['plugins_to_install'] = plugins_to_install.values()

    _validate_agent_plugins_on_host_nodes(processed_nodes)


def _post_process_node_relationships(node, node_name_to_node, plugins):
    if RELATIONSHIPS in node:
        for relationship in node[RELATIONSHIPS]:
            target_node = node_name_to_node[relationship['target_id']]
            _add_dependent(target_node, node)
            for interfaces in [SOURCE_INTERFACES, TARGET_INTERFACES]:
                if interfaces in relationship:
                    operations = {}
                    node_for_plugins = node if interfaces == SOURCE_INTERFACES else target_node
                    operations_attribute = 'source_operations' if interfaces == SOURCE_INTERFACES else \
                                           'target_operations'
                    for interface_name, interface in relationship[interfaces].items():
                        operation_mappings = _extract_plugin_names_and_operation_mapping_from_interface(interface,
                                                                                                        plugins)
                        for operation_name, plugin_name, operation_mapping in operation_mappings:
                            if plugin_name is not None:
                                node_for_plugins[PLUGINS][plugin_name] = _process_plugin(plugins[plugin_name],
                                                                                         plugin_name)
                                op_struct = _operation_struct(plugin_name, operation_mapping)
                                if operation_name in operations:
                                    # Indicate this implicit operation name needs to be removed as we can only
                                    # support explicit implementation in this case
                                    operations[operation_name] = None
                                else:
                                    operations[operation_name] = op_struct
                                operations['{0}.{1}'.format(interface_name, operation_name)] = op_struct
                            elif operation_mapping is not None:
                                # This means we couldn't find a matching plugin in the operation mapping
                                raise DSLParsingLogicException(19, 'Could not extract plugin from operation '
                                                                   'mapping {0}, '
                                                                   'which is declared for operation'
                                                                   ' {1} in interface {2}'
                                                                   ' in relationship of type {3} in node {4}'
                                                                   .format(operation_mapping,
                                                                           operation_name,
                                                                           interface_name,
                                                                           relationship['type'],
                                                                           node['id']))
                        _validate_no_duplicate_operations(operation_mappings, interface_name, node['id'], node['type'])
                    operations = dict((operation, op_struct)
                                      for operation, op_struct in operations.iteritems() if op_struct is not None)
                    relationship[operations_attribute] = operations


def _extract_plugin_names_and_operation_mapping_from_interface(interface, plugins):
    plugin_names = plugins.keys()
    result = []
    for operation in interface:
        operation_name, plugin_name, operation_mapping = _extract_plugin_name_and_operation_mapping_from_operation(
            plugin_names, operation)
        result.append((operation_name, plugin_name, operation_mapping))
    return result


def _add_dependent(node, dependent_node):
    dependents = node.get('dependents', [])
    #There can be two relation ships defined between same couple of nodes, avoid duplicate dependent
    if dependent_node['id'] in dependents:
        return
    dependents.append(dependent_node['id'])
    node['dependents'] = dependents


def _validate_agent_plugins_on_host_nodes(processed_nodes):
    for node in processed_nodes:
        if 'host_id' not in node and PLUGINS in node:
            for plugin in node[PLUGINS].itervalues():
                if plugin['agent_plugin'] == 'true':
                    raise DSLParsingLogicException(24, "node {0} has no relationship which makes it contained within "
                                                       "a host and it has an agent plugin named {1}, agent plugins "
                                                       "must be installed on a host".format(node['id'],
                                                                                            plugin['name']))


def _build_family_descendants_set(types_dict, derived_from):
    return {type_name for type_name in types_dict.iterkeys() if _is_derived_from(type_name, types_dict, derived_from)}


def _is_derived_from(type_name, types, derived_from):
    if type_name == derived_from:
        return True
    elif 'derived_from' in types[type_name]:
        return _is_derived_from(types[type_name]['derived_from'], types, derived_from)
    return False


#This method is applicable to both types and relationships. it's concerned with extracting the super types
#recursively, where the merging_func parameter is used to merge them with the current type
def _extract_complete_type_recursive(type_obj, type_name, dsl_container, merging_func, visited_type_names,
                                     is_relationships):
    if type_name in visited_type_names:
        visited_type_names.append(type_name)
        ex = DSLParsingLogicException(100, 'Failed parsing {0} {1}, Circular dependency detected: {2}'.format(
            'relationship' if is_relationships else 'type', type_name, ' --> '.join(visited_type_names)))
        ex.circular_dependency = visited_type_names
        raise ex
    visited_type_names.append(type_name)
    current_level_type = copy.deepcopy(type_obj)
    #halt condition
    if 'derived_from' not in current_level_type:
        return current_level_type

    super_type_name = current_level_type['derived_from']
    if super_type_name not in dsl_container:
        raise DSLParsingLogicException(14,
                                       'Missing definition for {0} {1} which is declared as derived by {0} {2}'
                                       .format('relationship' if is_relationships else 'type', super_type_name,
                                               type_name))

    super_type = dsl_container[super_type_name]
    complete_super_type = _extract_complete_type_recursive(super_type, super_type_name, dsl_container, merging_func,
                                                           visited_type_names, is_relationships)
    return merging_func(complete_super_type, current_level_type)


def _process_relationships(combined_parsed_dsl):
    processed_relationships = {}
    if RELATIONSHIPS not in combined_parsed_dsl:
        return processed_relationships

    relationships = combined_parsed_dsl[RELATIONSHIPS]

    for rel_name, rel_obj in relationships.iteritems():
        complete_rel_obj = _extract_complete_type_recursive(rel_obj, rel_name, relationships,
                                                            _rel_inheritance_merging_func, [], True)

        plugins = _get_dict_prop(combined_parsed_dsl, PLUGINS)
        _validate_relationship_fields(complete_rel_obj, plugins, rel_name)
        processed_relationships[rel_name] = copy.deepcopy(complete_rel_obj)
        processed_relationships[rel_name]['name'] = rel_name
        if 'derived_from' in processed_relationships[rel_name]:
            del (processed_relationships[rel_name]['derived_from'])

    return processed_relationships


def _validate_relationship_fields(rel_obj, plugins, rel_name):
    for interfaces in [SOURCE_INTERFACES, TARGET_INTERFACES]:
        if interfaces in rel_obj:
            for interface_name, interface in rel_obj[interfaces].items():
                mapping = _extract_plugin_names_and_operation_mapping_from_interface(interface, plugins)
                for operation_name, plugin_name, operation_mapping in mapping:
                    if plugin_name is None and operation_mapping is not None:
                        raise DSLParsingLogicException(19, 'Cannot find plugin matching operation {0}, '
                                                           'which is declared for relationship '
                                                           ' {1} ({2})'.format(operation_name,
                                                                               rel_name,
                                                                               operation_mapping))
                _validate_no_duplicate_operations(mapping, interface_name, relationship_name=rel_name)


def _rel_inheritance_merging_func(complete_super_type, current_level_type):
    merged_type = current_level_type
    
    # derived source and target interfaces
    for interfaces in [SOURCE_INTERFACES, TARGET_INTERFACES]:
        merged_interfaces = _merge_interface_dicts(complete_super_type, merged_type, interfaces)
        if len(merged_interfaces) > 0:
            merged_type[interfaces] = merged_interfaces

    return merged_type


def _merge_interface_dicts(overridden, overriding, interfaces_attribute):
    if interfaces_attribute not in overridden and interfaces_attribute not in overriding:
        return {}
    if interfaces_attribute not in overridden:
        return overriding[interfaces_attribute]
    if interfaces_attribute not in overriding:
        return overridden[interfaces_attribute]
    merged_interfaces = copy.deepcopy(overridden[interfaces_attribute])
    for overriding_interface, interface_obj in overriding[interfaces_attribute].items():
        interface_obj_copy = copy.deepcopy(interface_obj)
        if overriding_interface not in overridden[interfaces_attribute]:
            merged_interfaces[overriding_interface] = interface_obj_copy
        else:
            merged_interfaces[overriding_interface] = _merge_interface_list(
                overridden[interfaces_attribute][overriding_interface], interface_obj_copy)
    return merged_interfaces


def _merge_interface_list(overridden_interface, overriding_interface):

    def op_and_op_name(op):
        if type(op) == str:
            return op, op
        key, value = op.items()[0]
        return key, op

    # OrderedDict for easier testability
    overridden = OrderedDict((x, y) for x, y in map(op_and_op_name, overridden_interface))
    overriding = OrderedDict((x, y) for x, y in map(op_and_op_name, overriding_interface))
    result = []
    for op_name, operation in overridden.items():
        if op_name not in overriding:
            result.append(operation)
        else:
            result.append(overriding[op_name])
    for op_name, operation in overriding.items():
        if op_name not in overridden:
            result.append(operation)
    return result


def _extract_plugin_name_and_operation_mapping_from_operation(plugin_names, operation):
    if type(operation) == str:
        return operation, None, None
    operation_name, operation_mapping = operation.items()[0]
    longest_prefix = 0
    longest_prefix_plugin_name = None
    for plugin_name in plugin_names:
        if operation_mapping.startswith('{0}.'.format(plugin_name)):
            plugin_name_length = len(plugin_name)
            if plugin_name_length > longest_prefix:
                longest_prefix = plugin_name_length
                longest_prefix_plugin_name = plugin_name
    if longest_prefix_plugin_name is not None:
        return operation_name, longest_prefix_plugin_name, operation_mapping[longest_prefix+1:]
    else:
        # This is an error for validation done somewhere down the current stack trace
        return operation_name, None, operation_mapping


def _create_response_policies_section(processed_nodes):
    response_policies_section = {}
    for processed_node in processed_nodes:
        if POLICIES in processed_node:
            response_policies_section[processed_node['id']] = copy.deepcopy(processed_node[POLICIES])
    return response_policies_section


def _process_policies(policies):
    processed_policies_events = {}
    processed_rules = {}

    if 'types' in policies:
        for name, policy_event_obj in policies['types'].iteritems():
            processed_policies_events[name] = {}
            processed_policies_events[name]['message'] = policy_event_obj['message']
            processed_policies_events[name]['policy'] = _process_ref_or_inline_value(policy_event_obj, 'policy')
    if 'rules' in policies:
        for name, rule_obj in policies['rules'].iteritems():
            processed_rules[name] = copy.deepcopy(rule_obj)

    return processed_policies_events, processed_rules


def _process_workflows(workflows):
    processed_workflows = {}

    for name, flow_obj in workflows.iteritems():
        processed_workflows[name] = _process_ref_or_inline_value(flow_obj, 'radial')

    return processed_workflows


def _process_ref_or_inline_value(ref_or_inline_obj, inline_key_name):
    if 'ref' in ref_or_inline_obj:
        return ref_or_inline_obj['ref']
    else: #inline
        return ref_or_inline_obj[inline_key_name]


def _validate_no_duplicate_nodes(nodes):
    duplicate = _validate_no_duplicate_element(nodes, lambda node: node['name'])
    if duplicate is not None:
        ex = DSLParsingLogicException(101, 'Duplicate node definition detected, there are {0} nodes with name {'
                                           '1} defined'.format(duplicate[1], duplicate[0]))
        ex.duplicate_node_name = duplicate[0]
        raise ex


def _validate_no_duplicate_element(elements, keyfunc):
    elements.sort(key=keyfunc)
    groups = []
    from itertools import groupby

    for key, group in groupby(elements, key=keyfunc):
        groups.append(list(group))
    for group in groups:
        if len(group) > 1:
            return keyfunc(group[0]), len(group)


def _process_node_relationships(app_name, node, node_name, node_names_set, plugins, processed_node,
                                top_level_relationships):
    if RELATIONSHIPS in node:
        relationships = []
        for relationship in node[RELATIONSHIPS]:
            relationship_type = relationship['type']
            #validating only the instance relationship values - the inherited relationship values if any
            #should have been validated when the top level relationships were processed.
            #validate target field (done separately since it's only available in instance relationships)
            if relationship['target'] not in node_names_set:
                raise DSLParsingLogicException(25, 'a relationship instance under node {0} of type {1} declares an '
                                                   'undefined target node {2}'.format(node_name, relationship_type,
                                                                                      relationship['target']))
            if relationship['target'] == node_name:
                raise DSLParsingLogicException(23, 'a relationship instance under node {0} of type {1} '
                                                   'illegally declares the source node as the target node'.format(
                    node_name, relationship_type))
                #merge relationship instance with relationship type
            if relationship_type not in top_level_relationships:
                raise DSLParsingLogicException(26, 'a relationship instance under node {0} declares an undefined '
                                                   'relationship type {1}'.format(node_name, relationship_type))

            complete_relationship = _rel_inheritance_merging_func(top_level_relationships[relationship_type],
                                                                  relationship)
            #since we've merged with the already-processed top_level_relationships, there are a few changes that need
            #to take place - 'name' is replaced with a [fully qualified] 'target' field, and 'workflow' needs to be
            #re-processed if it is defined in 'relationship', since it overrides any possible already-processed
            #workflows that might have been inherited, and has not yet been processed
            complete_relationship['target_id'] = '{0}.{1}'.format(app_name, complete_relationship['target'])
            del (complete_relationship['target'])
            complete_relationship['state'] = 'reachable'
            relationships.append(complete_relationship)

        processed_node[RELATIONSHIPS] = relationships


def _autowire_node_type_name(node_type_name, types_descendants):

    def _autowire_node_type_name_recursive(node_type_name, current_path):
        descendants = types_descendants[node_type_name]
        if not descendants:
            return node_type_name

        if len(descendants) > 1:
            ex = DSLParsingLogicException(103, 'Ambiguous autowiring of type {0} detected, more than one candidate - {'
                                               '1}'.format(current_path[0], descendants))
            ex.descendants = list(descendants)
            raise ex
        autowire_candidate = descendants[0]
        if autowire_candidate in current_path:
            ex = DSLParsingLogicException(100, 'Failed parsing type {0}, Circular dependency detected: {1}'.format(
                current_path[0], ' --> '.join(current_path)))
            current_path.append(current_path[0])
            current_path.reverse()
            ex.circular_dependency = current_path
            raise ex

        current_path.append(autowire_candidate)
        return _autowire_node_type_name_recursive(autowire_candidate, current_path)

    return _autowire_node_type_name_recursive(node_type_name, [node_type_name])


def _validate_no_duplicate_operations(interface_operation_mappings,
                                      interface_name,
                                      node_id=None,
                                      node_type=None,
                                      relationship_name=None):
    operation_names = set()
    for operation_name, _, _ in interface_operation_mappings:
        if operation_name in operation_names:
            error_message = 'Duplicate operation {0} found in interface {1} '.format(operation_name, interface_name)
            if node_id is not None:
                error_message += ' in node {0} '.format(node_id)
            if node_type is not None:
                error_message += ' node type {0}'.format(node_type)
            if relationship_name is not None:
                error_message += ' relationship name {0}'.format(relationship_name)
            raise DSLParsingLogicException(20, error_message)
        operation_names.add(operation_name)


def _operation_struct(plugin_name, operation_mapping):
    return {'plugin': plugin_name, 'operation': operation_mapping}


def _process_node(node, parsed_dsl, top_level_policies_and_rules_tuple, top_level_relationships, node_names_set,
                  types_descendants, alias_mapping):
    declared_node_type_name = node['type']
    node_name = node['name']
    app_name = parsed_dsl[BLUEPRINT]['name']
    processed_node = {'id': '{0}.{1}'.format(app_name, node_name),
                      'declared_type': declared_node_type_name}

    #handle types
    if TYPES not in parsed_dsl or declared_node_type_name not in types_descendants:
        err_message = 'Could not locate node type: {0}; existing types: {1}'.format(declared_node_type_name,
                                                                                    parsed_dsl[TYPES].keys() if
                                                                                    TYPES in parsed_dsl else 'None')
        raise DSLParsingLogicException(7, err_message)

    node_type_name = _autowire_node_type_name(declared_node_type_name, types_descendants)
    processed_node['type'] = node_type_name

    node_type = parsed_dsl[TYPES][node_type_name]
    complete_node_type = _extract_complete_node_type(node_type, node_type_name, parsed_dsl, node)
    processed_node[PROPERTIES] = complete_node_type[PROPERTIES]
    processed_node[WORKFLOWS] = complete_node_type[WORKFLOWS]
    processed_node[POLICIES] = complete_node_type[POLICIES]

    #handle plugins and operations
    plugins = {}
    operations = {}
    if INTERFACES in complete_node_type:
        interface_objects = complete_node_type[INTERFACES]
        for interface_name, interface_object in interface_objects.items():
            mappings = _extract_plugin_names_and_operation_mapping_from_interface(interface_object,
                                                                                  parsed_dsl[PLUGINS])
            for operation_name, plugin_name, operation_mapping in mappings:
                if plugin_name is not None:
                    plugin = parsed_dsl[PLUGINS][plugin_name]
                    if not plugin_name in plugins:
                        plugins[plugin_name] = _process_plugin(plugin, plugin_name)
                    if operation_name in operations:
                        # Indicate this implicit operation name needs to be removed as we can only
                        # support explicit implementation in this case
                        operations[operation_name] = None
                    else:
                        operations[operation_name] = _operation_struct(plugin_name, operation_mapping)
                    operations['{0}.{1}'.format(interface_name, operation_name)] = _operation_struct(plugin_name,
                                                                                                     operation_mapping)
                # Operation mapping was defined for a non existent plugin
                elif operation_mapping is not None:
                    raise DSLParsingLogicException(10, 'Could not extract plugin from operation '
                                                       'mapping {0}, '
                                                       'which is declared for operation'
                                                       ' {1} in interface {2}'
                                                       ' in node {3} of type {4}'
                                                       .format(operation_mapping,
                                                               operation_name,
                                                               interface_name,
                                                               processed_node['id'],
                                                               processed_node['type']))
            _validate_no_duplicate_operations(mappings, interface_name, processed_node['id'], processed_node['type'])

        operations = dict((operation, op_struct)
                          for operation, op_struct in operations.iteritems() if op_struct is not None)
        processed_node[PLUGINS] = plugins
        processed_node['operations'] = operations

    #handle relationships
    _process_node_relationships(app_name, node, node_name, node_names_set, _get_dict_prop(parsed_dsl, PLUGINS),
                                processed_node, top_level_relationships)

    processed_node[PROPERTIES]['cloudify_runtime'] = {}
    processed_node[WORKFLOWS] = _process_workflows(processed_node[WORKFLOWS])
    _validate_node_policies(processed_node[POLICIES], node_name, top_level_policies_and_rules_tuple)

    processed_node['instances'] = node['instances'] if 'instances' in node else {'deploy': 1}

    return processed_node


def _extract_node_host_id(processed_node, node_name_to_node, host_types, contained_in_rel_types):
    if processed_node['type'] in host_types:
        return processed_node['id']
    else:
        if RELATIONSHIPS in processed_node:
            for rel in processed_node[RELATIONSHIPS]:
                if rel['type'] in contained_in_rel_types:
                    return _extract_node_host_id(node_name_to_node[rel['target_id']], node_name_to_node, host_types,
                                                 contained_in_rel_types)


def _process_plugin(plugin, plugin_name):
    #'cloudify.plugins.plugin'
    if plugin['derived_from'] not in ('cloudify.plugins.agent_plugin', 'cloudify.plugins.remote_plugin'):
        #TODO: consider changing the below exception to type DSLParsingFormatException..?
        raise DSLParsingLogicException(18, 'plugin {0} has an illegal "derived_from" value {1}; value must be'
                                           ' either {2} or {3}', plugin_name, plugin['derived_from'],
                                       'cloudify.plugins.agent_plugin', 'cloudify.plugins.remote_plugin')
    processed_plugin = copy.deepcopy(plugin[PROPERTIES])
    processed_plugin['name'] = plugin_name
    processed_plugin['agent_plugin'] = str(plugin['derived_from'] == 'cloudify.plugins.agent_plugin').lower()
    return processed_plugin


def _validate_node_policies(policies, node_name, top_level_policies_and_rules_tuple):
    #validating all policies and rules declared are indeed defined in the top level policies section
    for policy_obj in policies:
        policy_name = policy_obj['name']
        if policy_name not in top_level_policies_and_rules_tuple[0]:
            raise DSLParsingLogicException(16, 'Failed to parse node {0}: policy {1} not defined'.format(node_name,
                                                                                                         policy_name))
        for rule in policy_obj['rules']:
            if rule['type'] not in top_level_policies_and_rules_tuple[1]:
                raise DSLParsingLogicException(17, 'Failed to parse node {0}: rule {1} under policy {2} not '
                                                   'defined'.format(node_name, rule['type'], policy_name))


def _merge_sub_list(overridden_dict, overriding_dict, sub_list_key):
    def _get_named_list_dict(sub_list):
        return {entry['name']: entry for entry in sub_list}.items()

    overridden_sub_list = _get_list_prop(overridden_dict, sub_list_key)
    overriding_sub_list = _get_list_prop(overriding_dict, sub_list_key)
    name_to_list_entry = dict(_get_named_list_dict(overridden_sub_list) + _get_named_list_dict(overriding_sub_list))
    return name_to_list_entry.values()


def _merge_sub_dicts(overridden_dict, overriding_dict, sub_dict_key):
    overridden_sub_dict = _get_dict_prop(overridden_dict, sub_dict_key)
    overriding_sub_dict = _get_dict_prop(overriding_dict, sub_dict_key)
    return dict(overridden_sub_dict.items() + overriding_sub_dict.items())


def _extract_complete_node_type(dsl_type, dsl_type_name, parsed_dsl, node):
    def types_inheritance_merging_func(complete_super_type, current_level_type):
        merged_type = current_level_type
        #derive properties
        merged_type[PROPERTIES] = _merge_sub_dicts(complete_super_type, merged_type, PROPERTIES)
        #derive workflows
        merged_type[WORKFLOWS] = _merge_sub_dicts(complete_super_type, merged_type, WORKFLOWS)
        #derive policies
        merged_type[POLICIES] = _merge_sub_list(complete_super_type, merged_type, POLICIES)
        #derive interfaces
        merged_type[INTERFACES] = _merge_interface_dicts(complete_super_type, merged_type, INTERFACES)

        return merged_type

    complete_type = _extract_complete_type_recursive(dsl_type, dsl_type_name, parsed_dsl[TYPES],
                                                     types_inheritance_merging_func, [], False)
    return types_inheritance_merging_func(complete_type, copy.deepcopy(node))


def _apply_ref(filename, path_context, alias_mapping, resources_base_url):
    filename = _apply_alias_mapping_if_available(filename, alias_mapping)
    ref_url = _get_resource_location(filename, resources_base_url, path_context)
    if not ref_url:
        raise DSLParsingLogicException(31, 'Failed on ref - Unable to locate ref {0}'.format(filename))
    try:
        with contextlib.closing(urlopen(ref_url)) as f:
            return f.read()
    except URLError:
        raise DSLParsingLogicException(31, 'Failed on ref - Unable to open file {0} (searched for {1})'.format(
            filename, ref_url))


def _replace_or_add_interface(merged_interfaces, interface_element):
    #locate if this interface exists in the list
    matching_interface = next((x for x in merged_interfaces if _get_interface_name(x) == _get_interface_name(
        interface_element)), None)
    #add if not
    if matching_interface is None:
        merged_interfaces.append(interface_element)
    #replace with current interface element
    else:
        index_of_interface = merged_interfaces.index(matching_interface)
        merged_interfaces[index_of_interface] = interface_element


def _get_interface_name(interface_element):
    return interface_element if type(interface_element) == str else interface_element.iterkeys().next()


def _get_list_prop(dictionary, prop_name):
    return dictionary.get(prop_name, [])


def _get_dict_prop(dictionary, prop_name):
    return dictionary.get(prop_name, {})


def _autowire_plugin(plugins, interface_name, type_name):
    matching_plugins = [plugin_name for plugin_name, plugin_data in plugins.items() if
                        PROPERTIES in plugin_data and plugin_data[PROPERTIES]['interface'] == interface_name]

    num_of_matches = len(matching_plugins)
    if num_of_matches == 0:
        raise DSLParsingLogicException(11,
                                       'Failed to find a plugin which implements interface {0} as implicitly declared '
                                       'for type {1}'.format(interface_name, type_name))

    if num_of_matches > 1:
        raise DSLParsingLogicException(12,
                                       'Ambiguous implicit declaration for interface {0} implementation under type {'
                                       '1} - Found multiple matching plugins: ({2})'.format(interface_name, type_name,
                                                                                            ','.join(matching_plugins)))

    return matching_plugins[0]


def _combine_imports(parsed_dsl, alias_mapping, dsl_location, resources_base_url):
    def _merge_into_dict_or_throw_on_duplicate(from_dict, to_dict, top_level_key, path):
        for key, value in from_dict.iteritems():
            if key not in to_dict:
                to_dict[key] = value
            else:
                path.append(key)
                raise DSLParsingLogicException(4, 'Failed on import: Could not merge {0} due to conflict '
                                                  'on path {1}'.format(top_level_key, ' --> '.join(path)))

    #TODO: Find a solution for top level workflows, which should be probably somewhat merged with override
    merge_no_override = {INTERFACES, TYPES, PLUGINS, WORKFLOWS, RELATIONSHIPS}
    merge_one_nested_level_no_override = {POLICIES}

    combined_parsed_dsl = copy.deepcopy(parsed_dsl)
    _replace_ref_with_inline_paths(combined_parsed_dsl, dsl_location, alias_mapping, resources_base_url)

    if IMPORTS not in parsed_dsl:
        return combined_parsed_dsl

    _validate_imports_section(parsed_dsl[IMPORTS], dsl_location)

    ordered_imports_list = []
    _build_ordered_imports_list(parsed_dsl, ordered_imports_list, alias_mapping, dsl_location, resources_base_url)
    if dsl_location:
        ordered_imports_list = ordered_imports_list[1:]

    for single_import in ordered_imports_list:
        try:
            #(note that this check is only to verify nothing went wrong in the meanwhile, as we've already read
            # from all imported files earlier)
            with contextlib.closing(urlopen(single_import)) as f:
                parsed_imported_dsl = _load_yaml(f, 'Failed to parse import {0}'.format(single_import))
        except URLError, ex:
            error = DSLParsingLogicException(13, 'Failed on import - Unable to open import url {0}; {1}'.format(
                single_import, ex.message))
            error.failed_import = single_import
            raise error

        _replace_ref_with_inline_paths(parsed_imported_dsl, single_import, alias_mapping, resources_base_url)

        #combine the current file with the combined parsed dsl we have thus far
        for key, value in parsed_imported_dsl.iteritems():
            if key == IMPORTS: #no need to merge those..
                continue
            if key not in combined_parsed_dsl:
                #simply add this first level property to the dsl
                combined_parsed_dsl[key] = value
            else:
                if key in merge_no_override:
                    #this section will combine dictionary entries of the top level only, with no overrides
                    _merge_into_dict_or_throw_on_duplicate(value, combined_parsed_dsl[key], key, [])
                elif key in merge_one_nested_level_no_override:
                    #this section will combine dictionary entries on up to one nested level, yet without overrides
                    for nested_key, nested_value in value.iteritems():
                        if nested_key not in combined_parsed_dsl[key]:
                            combined_parsed_dsl[key][nested_key] = nested_value
                        else:
                            _merge_into_dict_or_throw_on_duplicate(nested_value, combined_parsed_dsl[key][nested_key],
                                                                   key, [nested_key])
                else:
                    #first level property is not white-listed for merge - throw an exception
                    raise DSLParsingLogicException(3, 'Failed on import: non-mergeable field {0}'.format(key))

    #clean the now unnecessary 'imports' section from the combined dsl
    if IMPORTS in combined_parsed_dsl:
        del combined_parsed_dsl[IMPORTS]
    return combined_parsed_dsl


def _replace_ref_with_inline_paths(dsl, path_context, alias_mapping, resources_base_url):
    if type(dsl) not in (list, dict):
        return

    if type(dsl) == list:
        for item in dsl:
            _replace_ref_with_inline_paths(item, path_context, alias_mapping, resources_base_url)
        return

    for key, value in dsl.iteritems():
        if key == 'ref':
            dsl[key] = _apply_ref(value, path_context, alias_mapping, resources_base_url)
        else:
            _replace_ref_with_inline_paths(value, path_context, alias_mapping, resources_base_url)


def _get_resource_location(resource_name, resources_base_url, current_resource_context=None):
    #Already url format
    if resource_name.startswith('http:') or resource_name.startswith('ftp:') or resource_name.startswith('file:'):
        return resource_name

    #Points to an existing file
    if os.path.exists(resource_name):
        return 'file:{0}'.format(pathname2url(resource_name))

    if current_resource_context:
        candidate_url = current_resource_context[:current_resource_context.rfind('/') + 1] + resource_name
        if _validate_url_exists(candidate_url):
            return candidate_url

    if resources_base_url:
        return resources_base_url + resource_name


def _validate_url_exists(url):
    try:
        with contextlib.closing(urlopen(url)):
            return True
    except URLError:
        return False


def _build_ordered_imports_list(parsed_dsl, ordered_imports_list, alias_mapping, current_import, resources_base_url):
    def _build_ordered_imports_list_recursive(parsed_dsl, current_import):
        if current_import is not None:
            ordered_imports_list.append(current_import)

        if IMPORTS not in parsed_dsl:
            return

        for another_import in parsed_dsl[IMPORTS]:
            another_import = _apply_alias_mapping_if_available(another_import, alias_mapping)
            import_url = _get_resource_location(another_import, resources_base_url, current_import)
            if import_url is None:
                ex = DSLParsingLogicException(13, 'Failed on import - no suitable location found for import {0}'.
                format(another_import))
                ex.failed_import = another_import
                raise ex
            if import_url not in ordered_imports_list:
                try:
                    with contextlib.closing(urlopen(import_url)) as f:
                        imported_dsl = _load_yaml(f, 'Failed to parse import {0} (via {1})'.format(another_import,
                                                                                                   import_url))
                    _build_ordered_imports_list_recursive(imported_dsl, import_url)
                except URLError, ex:
                    ex = DSLParsingLogicException(13, 'Failed on import - Unable to open import url {0}; {1}'.
                    format(import_url, ex.message))
                    ex.failed_import = import_url
                    raise ex

    _build_ordered_imports_list_recursive(parsed_dsl, current_import)


def _validate_dsl_schema(parsed_dsl):
    try:
        validate(parsed_dsl, DSL_SCHEMA)
    except ValidationError, ex:
        raise DSLParsingFormatException(1, '{0}; Path to error: {1}'.format(ex.message, '.'.join((str(x) for x in ex
        .path))))


def _validate_imports_section(imports_section, dsl_location):
    #imports section is validated separately from the main schema since it is validated for each file separately,
    #while the standard validation runs only after combining all imports together
    try:
        validate(imports_section, IMPORTS_SCHEMA)
    except ValidationError, ex:
        raise DSLParsingFormatException(2, 'Improper "imports" section in yaml {0}; {1}; Path to error: {2}'.format(
            dsl_location, ex.message, '.'.join((str(x) for x in ex.path))))


def _apply_alias_mapping_if_available(name, alias_mapping):
    return alias_mapping[name] if name in alias_mapping else name


class DSLParsingException(Exception):
    def __init__(self, err_code, *args):
        Exception.__init__(self, args)
        self.err_code = err_code


class DSLParsingLogicException(DSLParsingException):
    pass


class DSLParsingFormatException(DSLParsingException):
    pass