# Copyright (c) 2016 Cisco Systems
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import time

import click

from aim.agent.aid.universes.aci import tenant as aci_tenant
from aim.tools.cli.groups import aimcli


@aimcli.aim.command(name='spy-aci-tenant',
                    help='Simple application using event subscription for the'
                         'Tenant class. When run, this application will log '
                         'into the APIC and subscribe to events on the Tenant '
                         'class.  If a new tenant is created, the event will '
                         'be printed on the screen. Likewise, if an existing '
                         'tenant is deleted.')
@click.argument('tenant', required=True)
@click.pass_context
# Debug utility for ACI web socket
def spy_aci_tenant(ctx, tenant):
    tn = aci_tenant.AciTenantManager(tenant, ctx.obj['conf'].apic)
    tn._run()
    prev_state = None
    while True:
        time.sleep(1)
        curr_state = tn.get_state_copy()
        if curr_state != prev_state:
            click.echo(str(prev_state))
            prev_state = curr_state