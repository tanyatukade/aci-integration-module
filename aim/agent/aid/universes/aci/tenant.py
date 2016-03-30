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

import collections
import json
import time

from aim.agent.aid.universes.aci import converter
from aim.common.hashtree import structured_tree
from aim import exceptions

from acitoolkit import acitoolkit
from apicapi import apic_client
from apicapi import exceptions as apic_exc
import gevent
from oslo_log import log as logging

LOG = logging.getLogger(__name__)
TENANT_KEY = 'fvTenant'
STATUS_FIELD = 'status'
CHILDREN_LIST = set(converter.resource_map.keys() + ['fvTenant'])


# Dictionary with all the needed RS objects of a given APIC object type.
# Key is the APIC type, value is a list of DN suffixes that the manager needs
# to retrieve. For example, a value of ['rsctx'] for a fvBD means that the
# manager will do a GET on uni/tn-tname/BD-bdname/rsctx in order to complete
# the event list. A functor can be passed instead of a simple suffix list
# for more general cases.
def parent_dn(resource):
    dn = resource['attributes']['dn']
    return dn[:dn.rfind('/')]


RS_FILL_DICT = {'fvBD': ['rsctx'],
                'fvRsCtx': [parent_dn]}


class WebSocketSessionLoginFailed(exceptions.AimException):
    message = ("Web socket session failed to login for tenant %(tn_name)s "
               "with error %(code)s: %(text)s")


class WebSocketSubscriptionFailed(exceptions.AimException):
    message = ("Web socket session failed to subscribe for tenant %(tn_name)s "
               "with error %(code)s: %(text)s")


class Tenant(acitoolkit.Tenant):

    def __init__(self, *args, **kwargs):
        self.filtered_children = kwargs.pop('filtered_children', [])
        super(Tenant, self).__init__(*args, **kwargs)

    def _get_instance_subscription_urls(self):
        url = ('/api/mo/uni/tn-{}.json?query-target=subtree&'
               'rsp-prop-include=config-only&'
               'subscription=yes'.format(self.name))
        if self.filtered_children:
            url += '&target-subtree-class=' + ','.join(self.filtered_children)
        LOG.debug("Subscription url: %s", url)
        return [url]

    def instance_subscribe(self, session):
        # Have it publicly available
        resp = self._instance_subscribe(session)
        if resp.ok:
            return json.loads(resp.text)['imdata']
        else:
            raise WebSocketSubscriptionFailed(tn_name=self.name,
                                              code=resp.status_code,
                                              text=resp.text)

    def instance_unsubscribe(self, session):
        urls = self._get_instance_subscription_urls()
        for url in urls:
            session.unsubscribe(url)

    def instance_get_event_data(self, session):
        # Replace _instance_get_event to avoid object creation, we just need
        # the sheer data
        urls = self._get_instance_subscription_urls()
        for url in urls:
            # Aggregate similar events
            result = []
            while session.has_events(url):
                event = session.get_event(url)['imdata'][0]
                event_klass = event.keys()[0]
                event_dn = event[event_klass]['attributes']['dn']
                for partial in result:
                    if (event_klass == partial.keys()[0] and
                            event_dn == partial[event_klass][
                                'attributes']['dn']):
                        partial.update(event)
                        break
                else:
                    result.append(event)
            return result

    def instance_has_event(self, session):
        return self._instance_has_events(session)


class AciTenantManager(gevent.Greenlet):

    def __init__(self, tenant_name, apic_config, *args, **kwargs):
        super(AciTenantManager, self).__init__(*args, **kwargs)
        self.apic_config = apic_config
        # Each tenant has its own sessions
        self.aci_session = self._establish_aci_session(self.apic_config)
        self.dn_manager = apic_client.DNManager()
        self.tenant_name = tenant_name
        self.tenant = Tenant(self.tenant_name, filtered_children=CHILDREN_LIST)
        self._state = structured_tree.StructuredHashTree()
        self.health_state = False
        self.polling_yield = 1
        self.to_aim_converter = converter.AciToAimModelConverter()
        self.to_aci_converter = converter.AimToAciModelConverter()

    def is_dead(self):
        # Wrapping the greenlet property for easier testing
        return self.dead

    # This method is dangerous if run concurrently with _event_to_tree.
    # However, serialization/deserialization of the in-memory tree should not
    # cause I/O operation, therefore this method can't be context switched.
    def get_state_copy(self):
        return structured_tree.StructuredHashTree.from_string(str(self._state))

    def _run(self):
        try:
            while True:
                self._main_loop()
        except gevent.GreenletExit:
            try:
                self.tenant.instance_unsubscribe(self.ws_session)
            except Exception as e:
                LOG.error("An exception has occurred while exiting thread "
                          "for tenant %s: %s" % (self.tenant_name,
                                                 e.message))
            finally:
                # We need to make sure that this thread dies upon
                # GreenletExit
                return

    def _main_loop(self):
        try:
            # tenant subscription is redone upon exception
            self._subscribe_tenant()
            while True:
                self._event_loop()
        except gevent.GreenletExit:
            raise
        except Exception:
            import traceback
            LOG.error("An exception has occurred in thread serving tenant "
                      "%s" % self.tenant_name)
            LOG.error(traceback.format_exc())

    def _event_loop(self):
        start_time = time.time()
        if self.tenant.instance_has_event(self.ws_session):
            # Continuously check for events
            events = self.tenant.instance_get_event_data(
                self.ws_session)
            for event in events:
                if (event.keys()[0] == TENANT_KEY and not
                        event[TENANT_KEY]['attributes'].get(
                            STATUS_FIELD)):
                    # This is a full resync, tree needs to be reset
                    self._state = (
                        structured_tree.StructuredHashTree())
            LOG.debug("received events: %s", events)
            # Pull incomplete objects
            self._fill_events(events)
            LOG.debug("Filled events: %s", events)
            self._event_to_tree(events)
        # yield for other threads
        gevent.sleep(max(0, self.polling_yield - (time.time() -
                                                  start_time)))

    def push_aim_resources(self, resources):
        """Given a list of AIM resources for this tenant, push them into APIC

        :param resources: a dictionary with "create" and "delete" resources
        :return:
        """
        # Convert to ACI objects
        with self.aci_session.transaction() as trs:
            for method, aci_objects in resources.iteritems():
                # Method will be either "create" or "delete"
                aci_objects = self.to_aci_converter.convert(aci_objects)
                for obj in aci_objects:
                    # get MO from ACI client, identify it via its DN parts and
                    # push the new body
                    LOG.debug('%s ACI object %s' % (method, obj))
                    getattr(getattr(self.aci_session, obj.keys()[0]), method)(
                        *self.dn_manager.aci_decompose(
                            obj.values()[0]['attributes'].pop('dn'),
                            obj.keys()[0]),
                        transaction=trs,
                        **obj.values()[0]['attributes'])

    def _subscribe_tenant(self):
        # REVISIT(ivar): acitoolkit is missing some features like certificate
        # identification and multi controller support. In order to alleviate
        # this, we can at least simulate the multi controller here with the
        # following hacky code (which we can limiting to this method for now).
        # A decision should be taken whether we want to add features to
        # acitoolkit to at least support certificate identification, or if
        # we want to implement the WS interface in APICAPI altogether
        protocol = 'https' if self.apic_config.apic_use_ssl else 'http'
        if not getattr(self, 'ws_urls', None):
            self.ws_urls = collections.deque(
                ['%s://%s' % (protocol, host) for host in
                 self.apic_config.apic_hosts])
        self.ws_urls.rotate(-1)
        self.ws_session = acitoolkit.Session(
            self.ws_urls[0], self.apic_config.apic_username,
            self.apic_config.apic_password,
            verify_ssl=self.apic_config.verify_ssl_certificate)
        self.health_state = False
        resp = self.ws_session.login()
        if not resp.ok:
            raise WebSocketSessionLoginFailed(tn_name=self.tenant_name,
                                              code=resp.status_code,
                                              text=resp.text)
        self.tenant.instance_subscribe(self.ws_session)
        self.health_state = True

    def _establish_aci_session(self, apic_config):
        # TODO(IVAR): unnecessary things will be removed once apicapi gets its
        # own refactor.
        return apic_client.RestClient(
            logging,
            # TODO(ivar): retrieve APIC system ID
            '',
            apic_config.apic_hosts,
            apic_config.apic_username,
            apic_config.apic_password,
            apic_config.apic_use_ssl,
            scope_names=False,
            scope_infra=apic_config.scope_infra,
            renew_names=False,
            verify=apic_config.verify_ssl_certificate,
            request_timeout=apic_config.apic_request_timeout,
            cert_name=apic_config.certificate_name,
            private_key_file=apic_config.private_key_file,
            sign_algo=apic_config.signature_verification_algorithm,
            sign_hash=apic_config.signature_hash_type)

    def _event_to_tree(self, events):
        """Parse the event and push it into the tree

        This method requires translation between ACI and AIM model in order
        to  honor the Universe contract.
        :param events: an ACI event in the form of a list of objects
        :return:
        """
        # aim_objects = self.to_aim_converter.convert(events)
        # TODO(ivar): add nodes to the tree or remove them if needed

    def _fill_events(self, events):
        """Gets incomplete objects from APIC if needed

        - Objects with no status field are already completed
        - Objects with status "created" are already completed
        - Objects with status "deleted" do not exist on APIC anymore
        - Objects with status "modified" need to be retrieved fully via REST

        Whenever an object is missing on retrieval, status will be set to
        "deleted".
        Some objects might be incomplete without their RSs, this method
        will take care of retrieving them.
        :param events: List of events to retrieve
        :return:
        """
        extra_dns = set()
        visited = set()
        start = time.time()
        for event in events:
            if (event.values()[0]['attributes'].get(STATUS_FIELD) ==
                    converter.MODIFIED_STATUS):
                # 'dn' attribute is guaranteed to be there
                # TODO(ivar): the 'mo/' suffix should be added to APICAPI
                try:
                    # Remove from extra dns if there
                    resource = event.values()[0]
                    dn = resource['attributes']['dn']
                    extra_dns.discard(dn)
                    data = self.aci_session.get_data(
                        'mo/' + dn, rsp_prop_include='config-only')
                    resource['attributes'].update(
                        data[0].values()[0]['attributes'])
                    # See if there's any extra object to be retrieved
                    for filler in RS_FILL_DICT[event.keys()[0]]:
                        extra_dns.add(dn + '/' + filler if not callable(filler)
                                      else filler(resource))
                    visited.add(dn)
                except apic_exc.ApicResponseNotOk as e:
                    # Object might have been deleted
                    if e.err_code == '404':
                        LOG.debug("Resource %s not found", dn)
                        resource['attributes'][STATUS_FIELD] = (
                            converter.DELETED_STATUS)
                    else:
                        LOG.error(e.message)
                        raise
        # Process Extra DNs
        while extra_dns:
            try:
                # Get one DN
                dn = extra_dns.pop()
                if dn in visited:
                    # Avoid loops
                    continue
                # In case of IndexError, let's just rise and have the upper
                # layer taking care of the problem.
                data = self.aci_session.get_data(
                    'mo/' + dn, rsp_prop_include='config-only')[0]
                events.append(data)
                # See if there's any extra object to be retrieved
                for suffix in RS_FILL_DICT.get(data.keys()[0], []):
                    extra_dns.add(dn + '/' + suffix if not callable(suffix)
                                  else suffix(data.values()[0]))
                visited.add(dn)
            except apic_exc.ApicResponseNotOk as e:
                # Object might have been deleted or didn't exist
                # in a first place
                if e.err_code == '404':
                    LOG.debug("Resource %s not found", dn)
                else:
                    LOG.error(e.message)
                    raise
        LOG.debug('Filling procedure took %s for tenant %s' %
                  (time.time() - start, self.tenant.name))