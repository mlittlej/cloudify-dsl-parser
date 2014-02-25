########
# Copyright (c) 2014 GigaSpaces Technologies Ltd. All rights reserved
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
__author__ = 'dan'

import copy
import random

NODES = "nodes"


def create_multi_instance_plan(plan):
    """
    Expand node instances based on number of instances to deploy
    """
    plan = copy.deepcopy(plan)
    nodes = plan[NODES]

    new_nodes = []

    nodes_suffixes_map = _create_node_suffixes_map(nodes)
    node_ids = _create_node_suffixes_map(nodes).iterkeys()

    for node_id in node_ids:
        node = _get_node(node_id, nodes)
        instances = _create_node_instances(node, nodes_suffixes_map)
        new_nodes.extend(instances)

    plan[NODES] = new_nodes
    return plan


def _create_node_suffixes_map(nodes):
    """
    This method inspects the current nodes and creates a list of random
    suffixes.
    That is, for every node, it determines how many instances are needed
    and generates a random number (later used as id suffix) for each instance.
    """

    suffix_map = {}
    for node in nodes:
        if _is_host(node) or not _is_hosted(node):
            number_of_hosts = node["instances"]["deploy"]
            suffix_map[node["id"]] = _generate_unique_ids(number_of_hosts)

    for node in nodes:
        if not _is_host(node):
            if _is_hosted(node):
                host_id = node["host_id"]
                number_of_hosts = len(suffix_map[host_id])
                suffix_map[node["id"]] = _generate_unique_ids(number_of_hosts)
    return suffix_map


def _is_host(node):
    return _is_hosted(node) and node["host_id"] == node["id"]


def _is_hosted(node):
    return 'host_id' in node


def _get_node(node_id, nodes):
    """
    Retrieves a node from the nodes list based on the node id.
    """
    for node in nodes:
        if node_id == node['id']:
            return node
    raise RuntimeError("Could not find a node with id {0} in nodes"
                       .format(node_id))


def _create_node_instances(node, suffixes_map):
    """
    This method duplicates the given node 'number_of_instances' times and
    return an array with the duplicated instance.
    Each instance has a different id and each instance has a different host_id.
    id's are generated with an random index suffixed to the original id.
    For example: app.host --> [app.host_ab54ef, app.host_2_12345] in case of 2
     instances.
    """

    instances = []

    node_id = node['id']
    node_suffixes = suffixes_map[node_id]

    host_id = None
    if 'host_id' in node:
        host_id = node['host_id']
        host_suffixes = suffixes_map[host_id]
    number_of_instances = len(node_suffixes)

    #TODO: rewrite in parser properly (and make sure to change _instance_id
    # method)
    for i in range(number_of_instances):
        node_copy = node.copy()
        node_copy['id'] = _instance_id(node_id, node_suffixes[i])
        if host_id and host_suffixes:
            node_copy['host_id'] = _instance_id(host_id, host_suffixes[i])

        if 'relationships' in node_copy:
            new_relationships = []
            for relationship in node_copy['relationships']:
                target_id = relationship['target_id']
                if relationship['base'] == 'contained':
                    new_relationship = relationship.copy()
                    new_relationship['target_id'] = _instance_id(
                        target_id, suffixes_map[target_id][i])
                else:
                    new_relationship = relationship.copy()
                    # TODO support connected_to with tiers
                    # currently only 1 instance for connected_to
                    # (and depends_on) is supported
                    new_relationship['target_id'] = _instance_id(
                        target_id, suffixes_map[target_id][0])
                new_relationships.append(new_relationship)
            node_copy['relationships'] = new_relationships
        if 'dependents' in node_copy:
            new_dependents = []
            for dependent in node_copy['dependents']:
                new_dependents.append(_instance_id(
                    dependent, suffixes_map[dependent][i]))
            node_copy['dependents'] = new_dependents

        instances.append(node_copy)

    return instances


def _instance_id(node_id, node_suffix):
    return node_id + node_suffix if node_id != node_suffix else node_id


def _generate_unique_ids(number_of_ids):
    ids = []
    while len(ids) < number_of_ids:
        rand_id = '_%05x' % random.randrange(16 ** 5)
        if rand_id not in ids:
            ids.append(rand_id)

    return list(ids)
