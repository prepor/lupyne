"""
Restful json `CherryPy <http://cherrypy.org/>`_ server.

CherryPy and Lucene VM integration issues:
 * Monitors (such as autoreload) are not compatible with the VM unless threads are attached.
 * WorkerThreads must be also attached to the VM.
 * VM initialization must occur after daemonizing.
 * Recommended that the VM ignores keyboard interrupts (-Xrs) for clean server shutdown.
"""

from future_builtins import filter, map
import warnings
import re
import time
import httplib
import threading
import collections
import itertools, operator
import os, optparse
from email.utils import formatdate
from contextlib import contextmanager
try:
    import simplejson as json
except ImportError:
    import json
import lucene
import cherrypy
import engine

def tool(hook):
    "Return decorator to register tool at given hook point."
    def decorator(func):
        setattr(cherrypy.tools, func.__name__.rstrip('_'), cherrypy.Tool(hook, func))
        return func
    return decorator

@tool('before_handler')
def json_(indent=None, content_type='application/json', process_body=None):
    """Handle request bodies and responses in json format.

    :param indent: indentation level for pretty printing
    :param content_type: response content-type header
    :param process_body: optional function to process body into request.params
    """
    request = cherrypy.serving.request
    headers = cherrypy.response.headers
    if request.headers.get('content-type', '').endswith('json'):
        with HTTPError(httplib.BAD_REQUEST, ValueError):
            request.json = json.load(request.body)
        if process_body is not None:
            with HTTPError(httplib.BAD_REQUEST, TypeError):
                request.params.update(process_body(request.json))
    elif request.headers.get('content-type') == 'application/x-www-form-urlencoded':
        headers['Warning'] = '199 lupyne "Content-Type application/x-www-form-urlencoded has been deprecated and replaced with application/json"'
    handler = request.handler
    def json_handler(*args, **kwargs):
        body = handler(*args, **kwargs)
        if headers['content-type'].startswith('text/'):
            headers['content-type'] = content_type
            body = json.dumps(body, indent=indent)
        return body
    request.handler = json_handler

@tool('on_start_resource')
def allow(methods=('GET', 'HEAD')):
    "Only allow specified methods."
    request = cherrypy.serving.request
    if request.method not in methods and not isinstance(request.handler, cherrypy.HTTPError):
        cherrypy.response.headers['allow'] = ', '.join(methods)
        message = "The path {0!r} does not allow {1}.".format(request.path_info, request.method)
        raise cherrypy.HTTPError(httplib.METHOD_NOT_ALLOWED, message)

@tool('before_finalize')
def time_():
    "Return response time in headers."
    response = cherrypy.serving.response
    response.headers['x-response-time'] = time.time() - response.time

@tool('on_start_resource')
def validate(methods=('GET', 'HEAD'), etag=True, last_modified=True, max_age=None, expires=None):
    """Return and validate caching headers for GET requests.

    :param methods: only set headers for specified methods
    :param etag: return weak entity tag based on index version and validate if-match headers
    :param last_modified: return last-modified based on index timestamp and validate if-modified headers
    :param max_age: return cache-control max-age and age header based on last update
    :param expires: return expires header based on last update
    """
    request = cherrypy.serving.request
    headers = cherrypy.response.headers
    if request.method in methods and not isinstance(request.handler, cherrypy.HTTPError):
        if etag:
            headers['etag'] = 'W/"{0}"'.format(request.app.root.searcher.version)
            cherrypy.lib.cptools.validate_etags()
        if last_modified:
            headers['last-modified'] = formatdate(request.app.root.searcher.timestamp, usegmt=True)
            cherrypy.lib.cptools.validate_since()
        if max_age is not None:
            headers['age'] = int(time.time() - request.app.root.updated)
            headers['cache-control'] = 'max-age={0}'.format(max_age)
        if expires is not None:
            headers['expires'] = formatdate(expires + request.app.root.updated, usegmt=True)

def json_error(version, **body):
    "Transform errors into json format."
    tool = cherrypy.request.toolmaps['tools'].get('json', {})
    cherrypy.response.headers['content-type'] = tool.get('content_type', 'application/json')
    return json.dumps(body, indent=tool.get('indent'))

def attach_thread(id=None):
    "Attach current cherrypy worker thread to lucene VM."
    lucene.getVMEnv().attachCurrentThread()

class Autoreloader(cherrypy.process.plugins.Autoreloader):
    "Autoreload monitor compatible with lucene VM."
    def run(self):
        attach_thread()
        cherrypy.process.plugins.Autoreloader.run(self)

class AttachedMonitor(cherrypy.process.plugins.Monitor):
    "Periodically run a callback function in an attached thread."
    def __init__(self, bus, callback, frequency=cherrypy.process.plugins.Monitor.frequency):
        def run():
            attach_thread()
            callback()
        cherrypy.process.plugins.Monitor.__init__(self, bus, run, frequency)

@contextmanager
def HTTPError(status, *exceptions):
    "Interpret exceptions as an HTTPError with given status code."
    try:
        yield
    except exceptions as exc:
        raise cherrypy.HTTPError(status, str(exc))

class WebSearcher(object):
    "Dispatch root with a delegated Searcher."
    _cp_config = dict.fromkeys(map('tools.{0}.on'.format, ['gzip', 'json', 'allow', 'time', 'validate']), True)
    _cp_config.update({'error_page.default': json_error, 'tools.gzip.mime_types': ['text/html', 'text/plain', 'application/json']})
    def __init__(self, *directories, **kwargs):
        self.searcher = engine.MultiSearcher(directories, **kwargs) if len(directories) > 1 else engine.IndexSearcher(*directories, **kwargs)
        self.updated = time.time()
    @classmethod
    def new(cls, *args, **kwargs):
        "Return new uninitialized root which can be mounted on dispatch tree before VM initialization."
        self = object.__new__(cls)
        self.args, self.kwargs = args, kwargs
        return self
    def init(self, vmargs='-Xrs', **kwargs):
        "Callback to initialize VM and root object after daemonizing."
        lucene.initVM(vmargs=vmargs, **kwargs)
        self.__init__(*self.__dict__.pop('args'), **self.__dict__.pop('kwargs'))
    def close(self):
        self.searcher.close()
    @staticmethod
    def parse(searcher, q, **options):
        "Return parsed query using q.* parser options."
        options = dict((key.partition('.')[-1], options[key]) for key in options if key.startswith('q.'))
        field = options.pop('field', [])
        fields = [field] if isinstance(field, basestring) else field
        fields = [name.partition('^')[::2] for name in fields]
        if any(boost for name, boost in fields):
            field = dict((name, float(boost or 1.0)) for name, boost in fields)
        elif isinstance(field, basestring):
            (field, boost), = fields
        else:
            field = [name for name, boost in fields] or ''
        if 'type' in options:
            with HTTPError(httplib.BAD_REQUEST, AttributeError):
                return getattr(engine.Query, options['type'])(field, q)
        for key in set(options) - set(['op', 'version']):
            with HTTPError(httplib.BAD_REQUEST, ValueError):
                options[key] = json.loads(options[key])
        if q is not None:
            with HTTPError(httplib.BAD_REQUEST, lucene.JavaError):
                return searcher.parse(q, field=field, **options)
    @staticmethod
    def select(fields=None, **options):
        "Return parsed field selectors: stored, multi-valued, and indexed."
        if fields is not None:
            fields = dict.fromkeys(filter(None, fields.split(',')))
        if 'multifields' in options:
            options['fields.multi'] = options.pop('multifields')
            cherrypy.response.headers['Warning'] = '199 lupyne "multifields has been deprecated and renamed fields.multi"'
        multi = list(filter(None, options.get('fields.multi', '').split(',')))
        indexed = [field.split(':') for field in options.get('fields.indexed', '').split(',') if field]
        return fields, multi, indexed
    @cherrypy.expose
    @cherrypy.tools.allow(methods=['POST'])
    def refresh(self, **caches):
        raise cherrypy.HTTPRedirect(cherrypy.request.script_name + '/update', httplib.MOVED_PERMANENTLY)
    @cherrypy.expose
    @cherrypy.tools.json(process_body=dict.fromkeys)
    @cherrypy.tools.allow(methods=['POST'])
    def update(self, **caches):
        """Refresh index version.
        
        **POST** /update
            Reopen searcher, optionally reloading caches, and return document count.
            
            ["filters"|"sorters"|"spellcheckers",... ]
            
            :return: *int*
        """
        self.searcher = self.searcher.reopen(**dict.fromkeys(caches, True))
        self.updated = time.time()
        return len(self.searcher)
    @cherrypy.expose
    def index(self):
        """Return index information.
        
        **GET** /
            Return a mapping of the directory to the document count.
            
            :return: {*string*: *int*,... }
        """
        searcher = self.searcher
        if isinstance(searcher, lucene.MultiSearcher):
            return dict((unicode(reader.directory()), reader.numDocs()) for reader in searcher.sequentialSubReaders)
        return {unicode(searcher.directory): len(searcher)}
    @cherrypy.expose
    def docs(self, id=None, fields=None, **options):
        """Return ids or documents.
        
        **GET** /docs
            Return list of doc ids.
            
            :return: [*int*,... ]
        
        **GET** /docs/*int*?
            Return document mappings, optionally selecting stored, multi-valued, and cached indexed fields.
            
            &fields=\ *chars*,... &fields.multi=\ *chars*,... &fields.indexed=\ *chars*\ [:*chars*],...
            
            :return: {*string*: *string*\|\ *array*,... }
        """
        searcher = self.searcher
        if id is None:
            return list(searcher)
        fields, multi, indexed = self.select(fields, **options)
        with HTTPError(httplib.NOT_FOUND, ValueError, lucene.JavaError):
            id = int(id)
            doc = searcher[id] if fields is None else searcher.get(id, *itertools.chain(fields, multi))
        result = doc.dict(*multi, **(fields or {}))
        result.update((item[0], searcher.comparator(*item)[id]) for item in indexed)
        return result
    @cherrypy.expose
    def search(self, q=None, count=None, start=0, fields=None, sort=None, facets='', group='', hl='', mlt=None, spellcheck=0, timeout=None, **options):
        """Run query and return documents.
        
        **GET** /search?
            Return list of document objects and total doc count.
            
            &q=\ *chars*\ &q.type=[term|prefix|wildcard]&q.\ *chars*\ =...,
                query, optional type to skip parsing, and optional parser settings: q.field, q.op,...
            
            &filter=\ *chars*
                | cached filter applied to the query
                | if a previously cached filter is not found, the value will be parsed as a query
            
            &count=\ *int*\ &start=0
                maximum number of docs to return and offset to start at
            
            &fields=\ *chars*,... &fields.multi=\ *chars*,... &fields.indexed=\ *chars*\ [:*chars*],...
                only include selected stored fields; multi-valued fields returned in an array; indexed fields with optional type are cached
            
            &sort=\ [-]\ *chars*\ [:*chars*],... &sort.scores[=max]
                | field name, optional type, minus sign indicates descending
                | optionally score docs, additionally compute maximum score
            
            &facets=\ *chars*,...
                include facet counts for given field names; facets filters are cached
            
            &group=\ *chars*\ [:*chars*]&group.count=1&group.limit=\ *int*
                | group documents by field value with optional type, up to given maximum count
                | limit number of groups which return docs
            
            &hl=\ *chars*,... &hl.count=1&hl.tag=strong&hl.enable=[fields|terms]
                | stored fields to return highlighted
                | optional maximum fragment count and html tag name
                | optionally enable matching any field or any term
            
            &mlt=\ *int*\ &mlt.fields=\ *chars*,... &mlt.\ *chars*\ =...,
                | doc index (or id without a query) to find MoreLikeThis
                | optional document fields to match
                | optional MoreLikeThis settings: mlt.minTermFreq, mlt.minDocFreq,...
            
            &spellcheck=\ *int*
                | maximum number of spelling corrections to return for each query term, grouped by field
                | original query is still run; use q.spellcheck=true to affect query parsing
            
            &timeout=\ *number*
                timeout search after elapsed number of seconds
            
            :return:
                | {
                | "query": *string*,
                | "count": *int*\|null,
                | "maxscore": *number*\|null,
                | "docs": [{"__id__": *int*, "__score__": *number*, "__highlights__": {*string*: *array*,... }, *string*: *string*\|\ *array*,... },... ],
                | "facets": {*string*: {*string*: *int*,... },... },
                | "groups": [{"count": *int*, "value": *value*, "docs": [{... },... ]},... ]
                | "spellcheck": {*string*: {*string*: [*string*,... ],... },... },
                | }
        """
        searcher = self.searcher
        with HTTPError(httplib.BAD_REQUEST, ValueError):
            start = int(start)
            if count is not None:
                count = int(count) + start
            spellcheck = int(spellcheck)
            if timeout is not None:
                timeout = float(timeout)
            gcount = int(options.get('group.count', 1))
            glimit = int(options['group.limit']) if 'group.limit' in options else float('inf')
            hlcount = int(options.get('hl.count', 1))
            if mlt is not None:
                mlt = int(mlt)
        reverse = False
        if sort is not None:
            sort = (re.match('(-?)(\w+):?(\w*)', field).groups() for field in sort.split(','))
            sort = [(name, (type or 'string'), (reverse == '-')) for reverse, name, type in sort]
            if count is None:
                with HTTPError(httplib.BAD_REQUEST, ValueError, AttributeError):
                    reverse, = set(reverse for name, type, reverse in sort) # only one sort direction allowed with unlimited count
                    comparators = [searcher.comparator(name, type) for name, type, reverse in sort]
                sort = comparators[0].__getitem__ if len(comparators) == 1 else lambda id: tuple(map(operator.itemgetter(id), comparators))
            else:
                with HTTPError(httplib.BAD_REQUEST, AttributeError):
                    sort = [searcher.sorter(name, type, reverse=reverse) for name, type, reverse in sort]
        q = self.parse(searcher, q, **options)
        qfilter = options.pop('filter', None)
        if qfilter is not None and qfilter not in searcher.filters:
            searcher.filters[qfilter] = engine.Query.__dict__['filter'](self.parse(searcher, qfilter, **options))
        qfilter = searcher.filters.get(qfilter)
        if mlt is not None:
            if q is not None:
                mlt = searcher.search(q, count=mlt+1, sort=sort, reverse=reverse).ids[mlt]
            mltfields = filter(None, options.pop('mlt.fields', '').split(','))
            with HTTPError(httplib.BAD_REQUEST, ValueError):
                attrs = dict((key.partition('.')[-1], json.loads(options[key])) for key in options if key.startswith('mlt.'))
            q = searcher.morelikethis(mlt, *mltfields, **attrs)
        if count == 0:
            start = count = 1
        scores = options.get('sort.scores')
        scores = {'scores': scores is not None, 'maxscore': scores == 'max'}
        hits = searcher.search(q, filter=qfilter, count=count, sort=sort, reverse=reverse, timeout=timeout, **scores)[start:]
        result = {'query': q and unicode(q), 'count': hits.count, 'maxscore': hits.maxscore}
        tag = options.get('hl.tag', 'strong')
        field = 'fields' not in options.get('hl.enable', '') or None
        span = 'terms' not in options.get('hl.enable', '')
        if hl:
            hl = dict((name, searcher.highlighter(q, span=span, field=(field and name), formatter=tag)) for name in hl.split(','))
        fields, multi, indexed = self.select(fields, **options)
        if fields is None:
            fields = {}
        else:
            hits.fields = lucene.MapFieldSelector(list(itertools.chain(fields, multi, hl)))
            fields = fields or {'__id__': None}
        indexed = dict((item[0], searcher.comparator(*item)) for item in indexed)
        docs = []
        groups = collections.defaultdict(lambda: {'docs': [], 'count': 0, 'index': len(groups)})
        if group:
            with HTTPError(httplib.BAD_REQUEST, AttributeError):
                group = searcher.comparator(*group.split(':'))
            ids, scores = [], []
            for id, score in hits.items():
                item = groups[group[id]]
                item['count'] += 1
                if item['count'] <= gcount and item['index'] < glimit:
                    ids.append(id)
                    scores.append(score)
            hits.ids, hits.scores = ids, scores
        for hit in hits:
            doc = hit.dict(*multi, **fields)
            doc.update((name, indexed[name][hit.id]) for name in indexed)
            if hl:
                doc['__highlights__'] = dict((name, hl[name].fragments(hit[name], hlcount)) for name in hl if name in hit)
            (groups[group[hit.id]]['docs'] if group else docs).append(doc)
        for name in groups:
            groups[name]['value'] = name
        if group:
            result['groups'] = sorted(groups.values(), key=lambda item: item.pop('index'))
        else:
            result['docs'] = docs
        q = q or lucene.MatchAllDocsQuery()
        if facets:
            facets = (tuple(facet.split(':')) if ':' in facet else facet for facet in facets.split(','))
            result['facets'] = searcher.facets(q, *facets)
        if spellcheck:
            terms = result['spellcheck'] = collections.defaultdict(dict)
            for name, value in engine.Query.__dict__['terms'](q):
                terms[name][value] = list(itertools.islice(searcher.correct(name, value), spellcheck))
        return result
    @cherrypy.expose
    def terms(self, name='', value=':', *path, **options):
        """Return data about indexed terms.
        
        **GET** /terms?
            Return field names, with optional selection.
            
            &option=\ *chars*
            
            :return: [*string*,... ]
        
        **GET** /terms/*chars*\[:int|float\]?step=0
            Return term values for given field name, with optional type and step for numeric encoded values.
            
            :return: [*string*,... ]
        
        **GET** /terms/*chars*/*chars*\[\*\|?\|:*chars*\|~\ *number*\]
            Return term values (wildcards, slices, or fuzzy terms) for given field name.
            
            :return: [*string*,... ]
        
        **GET** /terms/*chars*/*chars*\[\*\|~\]?count=\ *int*
            Return spellchecked term values ordered by decreasing document frequency.
            Prefixes (*) are optimized to be suitable for real-time query suggestions; all terms are cached.
            
            :return: [*string*,... ]
        
        **GET** /terms/*chars*/*chars*
            Return document count with given term.
            
            :return: *int*
        
        **GET** /terms/*chars*/*chars*/docs
            Return document ids with given term.
            
            :return: [*int*,... ]
        
        **GET** /terms/*chars*/*chars*/docs/counts
            Return document ids and frequency counts for given term.
            
            :return: [[*int*, *int*],... ]
        
        **GET** /terms/*chars*/*chars*/docs/positions
            Return document ids and positions for given term.
            
            :return: [[*int*, [*int*,... ]],... ]
        """
        searcher = self.searcher
        if not name:
            return sorted(searcher.names(**options))
        if ':' in name:
            with HTTPError(httplib.BAD_REQUEST, ValueError, AttributeError):
                name, type = name.split(':')
                type = getattr(__builtins__, type)
                step = int(options.get('step', 0))
            return list(searcher.numbers(name, step=step, type=type))
        if ':' in value:
            with HTTPError(httplib.BAD_REQUEST, ValueError):
                start, stop = value.split(':')
            return list(searcher.terms(name, start, stop or None))
        if 'count' in options:
            with HTTPError(httplib.BAD_REQUEST, ValueError):
                count = int(options['count'])
            if value.endswith('*'):
                return searcher.suggest(name, value.rstrip('*'), count)
            if value.endswith('~'):
                return list(itertools.islice(searcher.correct(name, value.rstrip('~')), count))
        if '*' in value or '?' in value:
            return list(searcher.terms(name, value))
        if '~' in value:
            with HTTPError(httplib.BAD_REQUEST, ValueError):
                value, similarity = value.split('~')
                similarity = float(similarity or 0.5)
            return list(searcher.terms(name, value, minSimilarity=similarity))
        if not path:
            return searcher.count(name, value)
        if path[0] == 'docs':
            if path[1:] == ():
                return list(searcher.docs(name, value))
            if path[1:] == ('counts',):
                return list(searcher.docs(name, value, counts=True))
            if path[1:] == ('positions',):
                return list(searcher.positions(name, value))
        raise cherrypy.NotFound()

class WebIndexer(WebSearcher):
    "Dispatch root which extends searcher to include write methods."
    commit = WebSearcher.refresh
    def __init__(self, *args, **kwargs):
        self.indexer = engine.Indexer(*args, **kwargs)
        self.updated = time.time()
        self.lock = threading.Lock()
    @property
    def searcher(self):
        return self.indexer.indexSearcher
    def close(self):
        self.indexer.close()
        WebSearcher.close(self)
    @cherrypy.expose
    @cherrypy.tools.json(process_body=lambda body: {'directories': list(body)})
    @cherrypy.tools.allow(methods=['GET', 'HEAD', 'POST'])
    def index(self, directories=()):
        """Add indexes.  See :meth:`WebSearcher.index` for GET method.
        
        **POST** /
            Add indexes without optimization.
            
            [*string*,... ]
        """
        if cherrypy.request.method == 'POST':
            for directory in directories:
                self.indexer += directory
            cherrypy.response.status = httplib.ACCEPTED
        return {unicode(self.indexer.directory): len(self.indexer)}
    @cherrypy.expose
    @cherrypy.tools.json(process_body=dict.fromkeys)
    @cherrypy.tools.allow(methods=['POST'])
    def update(self, **options):
        """Commit index changes and refresh index version.
        
        **POST** /update
            Commit write operations and return document count.  See :meth:`WebSearcher.update` for caching options.
            
            ["expunge"|"optimize",... ]
            
            :return: *int*
        """
        with self.lock:
            self.indexer.commit(**dict.fromkeys(options, True))
        self.updated = time.time()
        return len(self.indexer)
    @cherrypy.expose
    @cherrypy.tools.json(process_body=lambda body: {'docs': body})
    @cherrypy.tools.allow(methods=['GET', 'HEAD', 'POST'])
    def docs(self, id=None, fields=None, docs=(), **options):
        """Add or return documents.  See :meth:`WebSearcher.docs` for GET method.
        
        **POST** /docs
            Add documents to index.
            
            [{*string*: *string*\|\ *array*,... },... ]
        """
        if cherrypy.request.method != 'POST':
            return WebSearcher.docs(self, id, fields, **options)
        with HTTPError(httplib.BAD_REQUEST, KeyError, ValueError): # deprecated
            if isinstance(docs, dict):
                docs = docs['docs']
            if isinstance(docs, basestring):
                docs = json.loads(docs)
        for doc in docs:
            self.indexer.add(doc)
        cherrypy.response.status = httplib.ACCEPTED
    @cherrypy.expose
    @cherrypy.tools.allow(methods=['GET', 'HEAD', 'DELETE'])
    def search(self, q=None, **options):
        """Run or delete a query.  See :meth:`WebSearcher.search` for GET method.
        
        **DELETE** /search?q=\ *chars*
            Delete documents which match query.
        """
        if cherrypy.request.method != 'DELETE':
            return WebSearcher.search(self, q, **options)
        self.indexer.delete(self.parse(self.searcher, q, **options) or lucene.MatchAllDocsQuery())
        cherrypy.response.status = httplib.ACCEPTED
    @cherrypy.expose
    @cherrypy.tools.json(process_body=dict)
    @cherrypy.tools.allow(methods=['GET', 'HEAD', 'PUT'])
    @cherrypy.tools.validate(on=False)
    def fields(self, name='', **settings):
        """Return or store a field's parameters.
        
        **GET** /fields
            Return known field names.
            
            :return: [*string*,... ]
        
        **GET, PUT** /fields/*chars*
            Set and return parameters for given field name.
            
            {"store"|"index"|"termvector": *string*\|true|false,... }
            
            :return: {"store": *string*, "index": *string*, "termvector": *string*}
        """
        if not name:
            allow()
            return sorted(self.indexer.fields)
        if cherrypy.request.method == 'PUT':
            if name not in self.indexer.fields:
                cherrypy.response.status = httplib.CREATED
            self.indexer.set(name, **settings)
        with HTTPError(httplib.NOT_FOUND, KeyError):
            field = self.indexer.fields[name]
        return dict((name, str(getattr(field, name))) for name in ['store', 'index', 'termvector'])

def start(root=None, path='', config=None, pidfile='', daemonize=False, autoreload=0, autoupdate=0, callback=None, autorefresh=0):
    """Attach root, subscribe to plugins, and start server.
    
    :param root,path,config: see cherrypy.quickstart
    :param pidfile,daemonize,autoreload,autoupdate: see command-line options
    :param callback: optional callback function scheduled after daemonizing
    """
    cherrypy.engine.subscribe('start_thread', attach_thread)
    if hasattr(root, 'close'):
        cherrypy.engine.subscribe('stop', root.close)
    cherrypy.config['engine.autoreload.on'] = False
    if pidfile:
        cherrypy.process.plugins.PIDFile(cherrypy.engine, os.path.abspath(pidfile)).subscribe()
    if daemonize:
        cherrypy.config['log.screen'] = False
        cherrypy.process.plugins.Daemonizer(cherrypy.engine).subscribe()
    if autoreload:
        Autoreloader(cherrypy.engine, autoreload).subscribe()
    if autorefresh:
        warnings.warn('Autorefresh has been renamed autoupdate.', DeprecationWarning)
        autoupdate = autorefresh
    if autoupdate:
        AttachedMonitor(cherrypy.engine, root.update, autoupdate).subscribe()
    if callback:
        priority = (cherrypy.process.plugins.Daemonizer.start.priority + cherrypy.server.start.priority) // 2
        cherrypy.engine.subscribe('start', callback, priority)
    cherrypy.quickstart(root, path, config)

parser = optparse.OptionParser(usage='python %prog [index_directory ...]')
parser.add_option('-r', '--read-only', action='store_true', help='expose only read methods; no write lock')
parser.add_option('-c', '--config', help='optional configuration file or json object of global params')
parser.add_option('-p', '--pidfile', metavar='FILE', help='store the process id in the given file')
parser.add_option('-d', '--daemonize', action='store_true', help='run the server as a daemon')
parser.add_option('--autoreload', type=int, metavar='SECONDS', help='automatically reload modules; replacement for engine.autoreload')
parser.add_option('--autoupdate', type=int, metavar='SECONDS', help='automatically update index version')
parser.add_option('--autorefresh', type=int, metavar='SECONDS', help='deprecated; use autoupdate')

if __name__ == '__main__':
    options, args = parser.parse_args()
    read_only = options.__dict__.pop('read_only')
    if options.config and not os.path.exists(options.config):
        options.config = {'global': json.loads(options.config)}
    cls = WebSearcher if (read_only or len(args) > 1) else WebIndexer
    root = cls.new(*map(os.path.abspath, args))
    start(root, callback=root.init, **options.__dict__)
