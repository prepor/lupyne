import unittest
import itertools
from lupyne import client
import remote

class TestCase(remote.BaseTest):
    ports = 8080, 8081, 8082
    hosts = map('localhost:{0:n}'.format, ports)
    def setUp(self):
        remote.BaseTest.setUp(self)
        self.servers = map(self.start, self.ports)
    def tearDown(self):
        for server in self.servers:
            self.stop(server)
        remote.BaseTest.tearDown(self)
    
    def testInterface(self):
        "Distributed reading and writing."
        resources = client.Resources(self.hosts)
        responses = resources.broadcast('GET', '/')
        assert len(responses) == len(resources)
        for response in responses:
            (directory, count), = response().items()
            assert count == 0 and directory.startswith('org.apache.lucene.store.RAMDirectory@')
        responses = resources.broadcast('PUT', '/fields/text')
        assert all(response() == {'index': 'ANALYZED', 'store': 'NO', 'termvector': 'NO'} for response in responses)
        responses = resources.broadcast('PUT', '/fields/name', {'store': 'yes', 'index': 'not_analyzed'})
        assert all(response() == {'index': 'NOT_ANALYZED', 'store': 'YES', 'termvector': 'NO'} for response in responses)
        doc = {'name': 'sample', 'text': 'hello world'}
        responses = resources.broadcast('POST', '/docs', {'docs': [doc]})
        assert all(response() == '' for response in responses)
        response = resources.unicast('POST', '/docs', {'docs': [doc]})
        assert response() == ''
        responses = resources.broadcast('POST', '/commit')
        assert all(response() >= 1 for response in responses)
        responses = resources.broadcast('GET', '/search?q=text:hello')
        docs = []
        for response in responses:
            result = response()
            assert result['count'] >= 1
            docs += result['docs']
        assert len(docs) == len(resources) + 1
        assert len(set(doc['__id__'] for doc in docs)) == 2
    
    def testSharding(self):
        "Sharding of indices across servers."
        shards = client.Shards(enumerate(itertools.combinations(self.hosts, 2)))
        shards.resources.broadcast('PUT', '/fields/zone', {'store': 'yes'})
        for zone in range(len(self.ports)):
            shards.broadcast(zone, 'POST', '/docs', {'docs': [{'zone': str(zone)}]})
        shards.resources.broadcast('POST', '/commit')
        result = shards.unicast(0, 'GET', '/search?q=zone:0')()
        assert result['count'] == len(result['docs']) == 1
        assert all(response() == result for response in shards.broadcast(0, 'GET', '/search?q=zone:0'))
        response, = shards.multicast([0], 'GET', '/search')
        assert set(doc['zone'] for doc in response()['docs']) > set('0')
        response, = shards.multicast([0, 1], 'GET', '/search')
        assert set(doc['zone'] for doc in response()['docs']) == set('01')
        zones = set()
        responses = shards.multicast([0, 1, 2], 'GET', '/search')
        assert len(responses) == 2
        for response in responses:
            docs = response()['docs']
            assert len(docs) == 2
            zones.update(doc['zone'] for doc in docs)
        assert zones == set('012')

if __name__ == '__main__':
    unittest.main()
