"""
Wrappers for lucene Index{Read,Search,Writ}ers.

The final `Indexer`_ classes exposes a high-level Searcher and Writer.
"""

import itertools, operator
import contextlib
from collections import defaultdict
import lucene
from .queries import Query, HitCollector, Filter
from .documents import Field, Document, Hits

def iterate(jit, positioned=False):
    """Transform java iterator into python iterator.
    
    :param positioned: current iterator position is valid
    """
    with contextlib.closing(jit):
        if positioned:
            yield jit
        while jit.next():
            yield jit

class IndexReader(object):
    """Delegated lucene IndexReader, with a mapping interface of ids to document objects.
    
    :param directory: lucene IndexReader or directory
    """
    def __init__(self, directory):
        self.indexReader = directory if isinstance(directory, lucene.IndexReader) else lucene.IndexReader.open(directory)
    def __getattr__(self, name):
        if name == 'indexReader':
            raise AttributeError(name)
        return getattr(self.indexReader, name)
    def __len__(self):
        return self.numDocs()
    def __contains__(self, id):
        return 0 <= id < self.maxDoc() and not self.isDeleted(id)
    def __iter__(self):
        return itertools.ifilterfalse(self.isDeleted, xrange(self.maxDoc()))
    def doc(self, id):
        return self.indexReader.document(id)
    def __getitem__(self, id):
        try:
            doc = self.doc(id)
        except lucene.JavaError:
            raise KeyError(id)
        return Document(doc)
    def __delitem__(self, id):
        self.deleteDocument(id)
    @property
    def directory(self):
        "reader's lucene Directory"
        return self.indexReader.directory()
    def delete(self, name, value):
        """Delete documents with given term.
        
        Acquires a write lock.  Deleting from an `IndexWriter`_ is encouraged instead.
        """
        self.deleteDocuments(lucene.Term(name, value))
    def count(self, name, value):
        "Return number of documents with given term."
        return self.docFreq(lucene.Term(name, value))
    def names(self, option='all'):
        "Return field names, given option description."
        option = getattr(self.FieldOption, option.upper())
        return list(self.getFieldNames(option))
    def terms(self, name, start='', stop=None, counts=False):
        "Generate a slice of term values, optionally with frequency counts."
        for termenum in iterate(self.indexReader.terms(lucene.Term(name, start)), positioned=True):
            term = termenum.term()
            if term and term.field() == name:
                text = term.text()
                if stop is None or text < stop:
                    yield (text, termenum.docFreq()) if counts else text
                    continue
            break
    def docs(self, name, value, counts=False):
        "Generate doc ids which contain given term, optionally with frequency counts."
        for termdocs in iterate(self.termDocs(lucene.Term(name, value))):
            doc = termdocs.doc()
            yield (doc, termdocs.freq()) if counts else doc
    def positions(self, name, value):
        "Generate doc ids which contain given term, with their positions."
        for termpositions in iterate(self.termPositions(lucene.Term(name, value))):
            positions = [termpositions.nextPosition() for n in xrange(termpositions.freq())]
            yield termpositions.doc(), positions
    def comparator(self, name, *names, **kwargs):
        """Return sequence of documents' field values suitable for sorting.

        :param names: additional names return tuples of values
        :param default: keyword only default value
        """
        if names:
            return zip(*(self.comparator(name, **kwargs) for name in (name,)+names))
        values = [kwargs.get('default')] * self.maxDoc()
        with contextlib.closing(self.termDocs()) as termdocs:
            for value in self.terms(name):
                termdocs.seek(lucene.Term(name, value))
                while termdocs.next():
                    values[termdocs.doc()] = value
        return values

class IndexSearcher(lucene.IndexSearcher, IndexReader):
    """Inherited lucene IndexSearcher, with a mixed-in IndexReader.
    
    :param directory: directory path or lucene Directory
    :param analyzer: lucene Analyzer class
    """
    def __init__(self, directory, analyzer=lucene.StandardAnalyzer):
        lucene.IndexSearcher.__init__(self, directory)
        self.analyzer = analyzer()
        self.filters = {}
    def __del__(self):
        if str(self) != '<null>':
            self.close()
    def parse(self, query, field='', op=''):
        """Return lucene parsed Query.
        
        :param field: default query field name
        :param op: default query operator ('or', 'and')
        """
        # parser's aren't thread-safe (nor slow), so create one each time
        parser = lucene.QueryParser(field, self.analyzer)
        if op:
            parser.defaultOperator = getattr(lucene.QueryParser.Operator, op.upper())
        return parser.parse(query)
    def facets(self, ids, *keys):
        """Return mapping of document counts for the intersection with each facet.
        
        :param ids: document ids
        :param keys: field names, term tuples, or any keys to previously cached filters
        """
        counts = defaultdict(dict)
        bits = Filter(ids).bits()
        for key in keys:
            filters = self.filters.get(key)
            if isinstance(filters, Filter):
                counts[key] = len(bits & filters.bits(self.indexReader))
            elif isinstance(key, basestring):
                values = self.terms(key) if filters is None else filters
                counts.update(self.facets(bits, *((key, value) for value in values)))
            else:
                name, value = key
                filters = self.filters.setdefault(name, {})
                if value not in filters:
                    filters[value] = Query.term(name, value).filter()
                counts[name][value] = len(bits & filters[value].bits(self.indexReader))
        return dict(counts)
    def count(self, *query, **options):
        """Return number of hits for given query or term.
        
        :param query: :meth:`search` compatible query, or optimally a name and value
        :param options: additional :meth:`search` options
        """
        if len(query) == 1:
            return self.search(query[0], count=1, **options).count
        return IndexReader.count(self, *query)
    def search(self, query=None, filter=None, count=None, sort=None, reverse=False, **parser):
        """Run query and return `Hits`_.
        
        :param query: query string or lucene Query
        :param filter: doc ids or lucene Filter
        :param count: maximum number of hits to retrieve
        :param sort: if count is given, lucene Sort parameters, else a callable key
        :param reverse: reverse flag used with sort
        :param parser: :meth:`parse` options
        """
        if query is None:
            query = lucene.MatchAllDocsQuery()
        elif not isinstance(query, lucene.Query):
            query = self.parse(query, **parser)
        if not isinstance(filter, (lucene.Filter, type(None))):
            filter = Filter(filter)
        # use custom HitCollector if all results are necessary, otherwise let lucene's TopDocs handle it
        if count is None:
            collector = HitCollector(self)
            lucene.IndexSearcher.search(self, query, filter, collector)
            return Hits(self, *collector.sorted(key=sort, reverse=reverse))
        if sort is None:
            topdocs = lucene.IndexSearcher.search(self, query, filter, count)
        else:
            if isinstance(sort, basestring):
                sort = lucene.Sort(sort, reverse)
            elif not isinstance(sort, lucene.Sort):
                sort = lucene.Sort(sort)
            topdocs = lucene.IndexSearcher.search(self, query, filter, count, sort)
        scoredocs = list(topdocs.scoreDocs)
        ids, scores = (map(operator.attrgetter(name), scoredocs) for name in ('doc', 'score'))
        return Hits(self, ids, scores, topdocs.totalHits)

class IndexWriter(lucene.IndexWriter):
    """Inherited lucene IndexWriter.
    Supports setting fields parameters explicitly, so documents can be represented as dictionaries.
    
    :param directory: directory path or lucene Directory
    :param mode: file mode (rwa), except updating (+) is implied
    :param analyzer: lucene Analyzer class
    """
    __len__ = lucene.IndexWriter.numDocs
    __del__ = IndexSearcher.__del__.im_func
    parse = IndexSearcher.parse.im_func
    def __init__(self, directory=None, mode='a', analyzer=lucene.StandardAnalyzer):
        create = [mode == 'w'] * (mode != 'a')
        lucene.IndexWriter.__init__(self, directory or lucene.RAMDirectory(), analyzer(), *create)
        self.fields = {}
    @property
    def segments(self):
        "segment filenames with document counts"
        items = (seg.split(':c') for seg in self.segString().split())
        return dict((name, int(value)) for name, value in items)
    def set(self, name, cls=Field, **params):
        """Assign parameters to field name.
        
        :param name: registered name of field
        :param cls: optional `Field`_ constructor
        :param params: store,index,termvector options compatible with `Field`_
        """
        self.fields[name] = cls(name, **params)
    def add(self, document=(), **terms):
        """Add document to index.
        Document is comprised of name: value pairs, where the values may be one or multiple strings.
        
        :param document: optional document terms as a dict or items
        :param terms: additional terms to document
        """
        terms.update(document)
        doc = lucene.Document()
        for name, values in terms.items():
            if isinstance(values, basestring):
                values = [values] 
            for field in self.fields[name].items(*values):
                doc.add(field)
        self.addDocument(doc)
    def delete(self, *query, **options):
        """Remove documents which match given query or term.
        
        :param query: :meth:`IndexSearcher.search` compatible query, or optimally a name and value
        :param options: additional :meth:`parse` options
        """
        if len(query) == 1:
            query, = query
            if not isinstance(query, lucene.Query):
                query = self.parse(query, **options)
            self.deleteDocuments(query)
        else:
            self.deleteDocuments(lucene.Term(*query))
    def __iadd__(self, directory):
        "Add directory (or reader, searcher, writer) to index."
        if isinstance(directory, basestring):
            directory = lucene.FSDirectory.getDirectory(directory)
        elif not isinstance(directory, lucene.Directory):
            directory = directory.directory
        self.addIndexesNoOptimize([directory])
        return self

class Indexer(IndexWriter):
    """An all-purpose interface to an index.
    Opening in read mode returns an `IndexSearcher`_.
    Opening in write mode (the default) returns an `IndexWriter`_ with a delegated `IndexSearcher`_.
    """
    def __new__(cls, directory=None, mode='a', analyzer=lucene.StandardAnalyzer):
        if mode == 'r':
            return IndexSearcher(directory, analyzer)
        return IndexWriter.__new__(cls)
    def __init__(self, *args, **kwargs):
        IndexWriter.__init__(self, *args, **kwargs)
        self.indexSearcher = IndexSearcher(self.directory, self.getAnalyzer)
    def __getattr__(self, name):
        if name == 'indexSearcher':
            raise AttributeError(name)
        return getattr(self.indexSearcher, name)
    def __contains__(self, id):
        return id in self.indexSearcher
    def __iter__(self):
        return iter(self.indexSearcher)
    def __getitem__(self, id):
        return self.indexSearcher[id]
    def commit(self):
        "Commit writes and refresh searcher.  Not thread-safe."
        IndexWriter.commit(self)
        if not self.current:
            self.indexSearcher = IndexSearcher(self.directory, self.getAnalyzer)
