"""
Restful json clients.

Use `Resource`_ for a single host.
Use `Resources`_ for multiple hosts with simple partitioning or redundancy.
Use `Shards`_ for horizontally partitioning hosts by different keys.

`Resources`_ reuse persistent connections as possible, handling request timeouts.
Broadcasting to multiple resources is parallelized with asynchronous requests and responses.

The load balancing strategy is randomized, biased by the number of cached connections available.
This inherently provides limited failover support, but applications must still handle exceptions as desired.
"""

import gzip
import random
import itertools
from cStringIO import StringIO
import httplib, urllib
try:
    import simplejson as json
except ImportError:
    import json

class Response(httplib.HTTPResponse):
    "A completed response which handles json and caches its body."
    def begin(self):
        httplib.HTTPResponse.begin(self)
        self.body = self.read()
        if self.getheader('content-encoding') == 'gzip':
            self.body = gzip.GzipFile(fileobj=StringIO(self.body)).read()
    def __call__(self):
        "Return evaluated response body or raise exception."
        if not httplib.OK <= self.status < httplib.MULTIPLE_CHOICES:
            raise httplib.HTTPException(self.status, self.reason, self.body)
        if self.body and self.getheader('content-type') == 'text/x-json':
            return json.loads(self.body)
        return self.body

class Resource(httplib.HTTPConnection):
    "Synchronous connection which handles json responses."
    response_class = Response
    def request(self, method, path, body=None):
        "Send request after handling body and headers."
        headers = {'accept-encoding': 'compress, gzip', 'content-length': 0}
        if body is not None:
            body = urllib.urlencode(dict((name, value if isinstance(value, basestring) else json.dumps(value)) \
                for name, value in body.items()))
            headers.update({'content-length': len(body), 'content-type': 'application/x-www-form-urlencoded'})
        httplib.HTTPConnection.request(self, method, path, body, headers)
    def __call__(self, method, path, body=None):
        "Send request and return evaluated response body."
        self.request(method, path, body)
        return self.getresponse()()
    def get(self, path='/', **params):
        if params:
            path += '?' + urllib.urlencode(params)
        return self('GET', path)
    def post(self, path, **body):
        return self('POST', path, body)
    def put(self, path, **body):
        return self('PUT', path, body)
    def delete(self, path):
        return self('DELETE', path)

class Resources(dict):
    "Thread-safe mapping of hosts to persistent resources."
    def __init__(self, hosts):
        self.update((host, []) for host in hosts)
    def pop(self, host):
        "Return an exclusive resource for given host."
        try:
            return self[host].pop()
        except IndexError:
            return Resource(host)
    def push(self, host, resource):
        "Put resource back into available pool."
        self[host].append(resource)
    def request(self, host, method, path, body=None):
        "Send request to given host and return exclusive resource."
        resource = self.pop(host)
        resource.request(method, path, body)
        return resource
    def unicast(self, method, path, body=None, hosts=()):
        "Send request and return response from any host, optionally from given subset."
        hosts = tuple(hosts) or self
        candidates = itertools.chain.from_iterable([host] * len(self[host]) for host in hosts)
        host = random.choice(list(candidates) or tuple(hosts))
        resource = self.request(host, method, path, body)
        response = resource.getresponse()
        if response.status == httplib.REQUEST_TIMEOUT:
            return self.unicast(method, path, body, hosts)
        self.push(host, resource)
        return response
    def broadcast(self, method, path, body=None, hosts=()):
        "Send requests and return responses from all hosts, optionally from given subset."
        hosts = tuple(hosts) or self
        responses = {}
        resources = dict((host, self.request(host, method, path, body)) for host in hosts)
        while resources:
            for host in list(resources):
                resource = resources.pop(host)
                response = responses[host] = resource.getresponse()
                if response.status == httplib.REQUEST_TIMEOUT:
                    resources[host] = self.request(host, method, path, body)
                else:
                    self.push(host, resource)
        return map(responses.__getitem__, hosts)

class Shards(dict):
    "Mapping of keys to host clusters, with associated resources."
    def __init__(self, mapping=(), **kwargs):
        for key, hosts in dict(mapping, **kwargs).items():
            self[key] = set(hosts)
        self.resources = Resources(itertools.chain(*self.values()))
    def unicast(self, key, method, path, body=None):
        "Send request and return response from any host for corresponding key."
        return self.resources.unicast(method, path, body, self[key])
    def broadcast(self, key, method, path, body=None):
        "Send requests and return responses from all hosts for corresponding key."
        return self.resources.broadcast(method, path, body, self[key])
    def multicast(self, keys, method, path, body=None):
        """Send requests and return responses from a subset of hosts which cover all corresponding keys.
        A minimal number of hosts will be called, but overlap is possible depending on partitioning.
        """
        shards = set(map(frozenset, itertools.product(*map(self.__getitem__, keys))))
        size = min(map(len, shards))
        shards = [hosts for hosts in shards if len(hosts) <= size]
        candidates = []
        for hosts in shards:
            candidates += [hosts] * min(len(self.resources[host]) for host in hosts)
        hosts = random.choice(candidates or shards)
        return self.resources.broadcast(method, path, body, hosts)
