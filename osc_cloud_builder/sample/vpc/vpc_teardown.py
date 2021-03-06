#!/usr/bin/python
# -*- coding: utf-8 -*-

# Copyright (c) 2016, Outscale SAS
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
#
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF

"""
Destroy a VPC and all attached ressources
"""

__author__      = "Heckle"
__copyright__   = "BSD"


import time
from boto.ec2.ec2object import EC2Object
from osc_cloud_builder.OCBase import OCBase, SLEEP_SHORT
from osc_cloud_builder.tools.wait_for import wait_state
from boto.exception import EC2ResponseError

def teardown(vpc_to_delete, terminate_instances=False):
    """
    Clean all ressouces attached to the vpc_to_delete
    :param vpc_to_delete: vpc id to delete
    :type vpc_to_delete: str
    :param terminate_instances: continue teardown even if instances exists in the VPC
    :type terminate_instances: bool
    """
    ocb = OCBase()

    if terminate_instances is False and \
       ocb.fcu.get_only_instances(filters={'vpc-id': vpc_to_delete, 'instance-state-name': 'running'}) and \
       ocb.fcu.get_only_instances(filters={'vpc-id': vpc_to_delete, 'instance-state-name': 'stopped'}) :
        ocb.log('Instances still exist in {0}, teardown will not be executed. Add terminate_instances=True to teardown() method.'.format(vpc_to_delete) ,'error')
        return

    ocb.log('Deleting VPC {0}'.format(vpc_to_delete), 'info', __file__)
    vpc_instances = ocb.fcu.get_only_instances(filters={'vpc-id': vpc_to_delete})
    ocb.log('Termating VMs {0}'.format(vpc_instances), 'info')

    # Stop instances
    if [instance for instance in vpc_instances if instance.state != 'stopped' or instance.state != 'terminated']:
        try:
            ocb.fcu.stop_instances([instance.id for instance in vpc_instances])
        except EC2ResponseError as err:
            ocb.log('Stop instance error: {0}'.format(err.message), 'warning')

    time.sleep(SLEEP_SHORT)

    # Force stop instances (if ACPI STOP does not work)
    if [instance for instance in vpc_instances if instance.state != 'stopped' or instance.state != 'terminated']:
        try:
            ocb.fcu.stop_instances([instance.id for instance in vpc_instances], force=True)
        except EC2ResponseError as err:
            ocb.log('Force stop instance error: {0}'.format(err.message), 'warning')

    # Wait instance to be stopped
    wait_state(vpc_instances, 'stopped')

    # Terminate instances
    if [instance for instance in vpc_instances if instance.state != 'terminated']:
        try:
            ocb.fcu.terminate_instances([instance.id for instance in vpc_instances])
        except EC2ResponseError as err:
            ocb.log('Terminate instance error: {0}'.format(err.message), 'warning')

    # Wait instance to be terminated
    wait_state(vpc_instances, 'terminated')

    # Delete VPC-Peering connections
    for peer in ocb.fcu.get_all_vpc_peering_connections(filters={'requester-vpc-info.vpc-id': vpc_to_delete}):
        peer.delete()

    # Delete VPC Endpoints - Not able to manage multiple vpce
    try:
        vpc_endpoint = ocb.fcu.get_object('DescribeVpcEndpoints', {'Filter.0.Name': 'vpc-id', 'Filter.0.Value': vpc_to_delete,
                                                                   'Filter.1.Name': 'vpc-endpoint-state', 'Filter.1.Value': 'available'}, EC2Object)
        ocb.fcu.make_request('DeleteVpcEndpoints', {'VpcEndpointId.0': vpc_endpoint.vpcEndpointId}).read()
    except Exception as err:
        ocb.log('Can not delete Vpc Endpoints', 'warning')


    # Release EIPs
    for instance in vpc_instances:
        addresses = ocb.fcu.get_all_addresses(filters={'instance-id': instance.id})
        for address in addresses:
            try:
                ocb.fcu.disassociate_address(association_id=address.association_id)
            except EC2ResponseError as err:
                ocb.log('Disassociate EIP error: {0}'.format(err.message), 'warning')
            time.sleep(SLEEP_SHORT)
            try:
                ocb.fcu.release_address(allocation_id=address.allocation_id)
            except EC2ResponseError as err:
                ocb.log('Release EIP error: {0}'.format(err.message), 'warning')

        time.sleep(SLEEP_SHORT)

    # Flush all nic
    for nic in ocb.fcu.get_all_network_interfaces(filters={'vpc-id': vpc_to_delete}):
        nic.delete()


    # Delete internet gateways
    for gw in ocb.fcu.get_all_internet_gateways(filters={'attachment.vpc-id': vpc_to_delete}):
        for attachment in gw.attachments:
            ocb.fcu.detach_internet_gateway(gw.id, attachment.vpc_id)
            time.sleep(SLEEP_SHORT)
        ocb.fcu.delete_internet_gateway(gw.id)

    time.sleep(SLEEP_SHORT)

    try:
        # Delete nat gateways
        # get_object is not able to manage a collection, so using subnet-id as differentiating
        ocb.fcu.APIVersion = '2016-11-15'
        for msubnet in ocb.fcu.get_all_subnets(filters={'vpc-id': vpc_to_delete}):
            nat_gateway = ocb.fcu.get_object('DescribeNatGateways', {'Filter.1.Name': 'vpc-id', 'Filter.1.Value.1': vpc_to_delete, 'Filter.2.Name': 'subnet-id', 'Filter.2.Value.1': msubnet.id}, EC2Object)
            if hasattr(nat_gateway, 'natGatewayId'):
                ocb.fcu.make_request('DeleteNatGateway', params={'NatGatewayId': nat_gateway.natGatewayId})
                ocb.log('Deleting natGateway {0}'.format(nat_gateway.natGatewayId), 'info')
    except Exception as err:
        ocb.log('Can not delete natgateway because: {0}'.format(err.message), 'warning')

    # Delete routes
    for rt in ocb.fcu.get_all_route_tables(filters={'vpc-id': vpc_to_delete}):
        for route in rt.routes:
            if route.gateway_id != 'local':
                try:
                    ocb.fcu.delete_route(rt.id, route.destination_cidr_block)
                except Exception as err:
                    ocb.log('Can not delete route {0} because {1}'.format(route.destination_cidr_block, err), 'warning')


    # Delete Load Balancers
    if ocb.lbu:
        subnets = set([sub.id for sub in ocb.fcu.get_all_subnets(filters={'vpc-id': vpc_to_delete})])
        for lb in [lb for lb in ocb.lbu.get_all_load_balancers() if set(lb.subnets).intersection(subnets)]:
            lb.delete()
            time.sleep(SLEEP_SHORT)

        # Wait for load balancers to disapear
        for i in range(1, 420):          # 42 ? Because F...
            lbs = [lb for lb in ocb.lbu.get_all_load_balancers() if set(lb.subnets).intersection(subnets)]
            if not lbs:
                break
            time.sleep(SLEEP_SHORT)
            ocb.log('Waiting for LBU {0} to be removed'.format(lbs), 'info')

    for vpc in ocb.fcu.get_all_vpcs([vpc_to_delete]):
        # Delete route tables
        for route_table in ocb.fcu.get_all_route_tables(filters={'vpc-id': vpc.id}):
            for association in route_table.associations:
                if association.subnet_id:
                    ocb.fcu.disassociate_route_table(association.id)
        for route_table in [route_table for route_table
                            in ocb.fcu.get_all_route_tables(filters={'vpc-id': vpc.id})
                            if len([association for association in route_table.associations if association.main]) == 0]:
            ocb.fcu.delete_route_table(route_table.id)

        # Delete subnets
        for subnet in ocb.fcu.get_all_subnets(filters={'vpc-id': vpc.id}):
            ocb.fcu.delete_subnet(subnet.id)

    time.sleep(SLEEP_SHORT)

    # Flush all rules
    for group in ocb.fcu.get_all_security_groups(filters={'vpc-id': vpc.id}):
        for rule in group.rules:
            for grant in rule.grants:
                ocb.fcu.revoke_security_group(group_id=group.id, ip_protocol=rule.ip_protocol, from_port=rule.from_port, to_port=rule.to_port, src_security_group_group_id=grant.group_id, cidr_ip=grant.cidr_ip)
            for rule in group.rules_egress:
                for grant in rule.grants:
                    ocb.fcu.revoke_security_group_egress(group.id, rule.ip_protocol, rule.from_port, rule.to_port, grant.group_id, grant.cidr_ip)

    # Delete Security Groups
    for sg in ocb.fcu.get_all_security_groups(filters={'vpc-id': vpc.id}):
        if 'default' not in sg.name:
            try:
                ocb.fcu.delete_security_group(group_id=sg.id)
            except EC2ResponseError as err:
                ocb.log('Can not delete Security Group: {0}'.format(err.message), 'warning')


    # Delete VPC
    try:
        ocb.fcu.delete_vpc(vpc.id)
    except EC2ResponseError as err:
        ocb.log('Can not delete VPC: {0}'.format(err.message), 'error')
