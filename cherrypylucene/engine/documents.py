"""
Wrappers for lucene Fields and Documents.
"""

import itertools
import lucene
from .queries import Query

class Field(object):
    """Saved parameters which can generate lucene Fields given values.
    
    :param name: name of field
    :param store, index, termvector: field parameters, expressed as bools or strs, with lucene defaults
    """
    def __init__(self, name, store=False, index='analyzed', termvector=False):
        self.name = name
        if isinstance(store, bool):
            store = 'yes' if store else 'no'
        if isinstance(index, bool):
            index = 'not_analyzed' if index else 'no'
        if isinstance(termvector, bool):
            termvector = 'yes' if termvector else 'no'
        for name, value in zip(['Store', 'Index', 'TermVector'], [store, index, termvector]):
            setattr(self, name.lower(), getattr(getattr(lucene.Field, name), value.upper()))
    def items(self, *values):
        "Generate lucene Fields suitable for adding to a document."
        for value in values:
            yield lucene.Field(self.name, value, self.store, self.index, self.termvector)

class PrefixField(Field):
    """Field which indexes every prefix of a value into a separate component field.
    The customizable component field names are expressed as slices.
    Original value may be stored only for convenience.
    
    :param start, stop, step: optional slice parameters of the prefix depths (not indices)
    """
    def __init__(self, name, start=1, stop=None, step=1, store=False, index=True, termvector=False):
        Field.__init__(self, name, store, index, termvector)
        self.depths = slice(start, stop, step)
    def split(self, text):
        "Return immutable sequence of words from name or value."
        return text
    def join(self, words):
        "Return text from separate words."
        return words
    def getname(self, depth):
        "Return prefix field name for given depth."
        return '{0}[:{1:n}]'.format(self.name, depth)
    def indices(self, depth):
        "Return range of valid depth indices."
        return xrange(*self.depths.indices(depth + self.depths.step))
    def items(self, *values):
        """Generate indexed component fields.
        Optimized to handle duplicate values.
        """
        if self.store != lucene.Field.Store.NO:
            for value in values:
                yield lucene.Field(self.name, value, self.store, lucene.Field.Index.NO)
        values = map(self.split, values)
        for depth in self.indices(max(map(len, values))):
            name = self.getname(depth)
            for value in sorted(set(value[:depth] for value in values if len(value) > (depth-self.depths.step))):
                yield lucene.Field(name, self.join(value), lucene.Field.Store.NO, self.index, self.termvector)
    def prefix(self, value):
        "Return prefix query of the closest possible prefixed field."
        depths = self.indices(len(self.split(value)))
        depth = depths[-1] if depths else self.depths.start
        return Query.prefix(self.getname(depth), value)
    def range(self, start, stop, lower=True, upper=False):
        "Return range query of the closest possible prefixed field."
        depths = self.indices(max(len(self.split(value)) for value in (start, stop)))
        depth = depths[-1] if depths else self.depths.start
        return Query.range(self.getname(depth), start, stop, lower, upper)

class NestedField(PrefixField):
    """Field which indexes every component into its own field.
    
    :param sep: field separator used on name and values
    """
    def __init__(self, name, sep=':', **kwargs):
        PrefixField.__init__(self, name, **kwargs)
        self.sep = sep
        names = self.split(name)
        self.names = [self.join(names[:depth]) for depth in range(len(names)+1)]
    def split(self, text):
        "Return immutable sequence of words from name or value."
        return tuple(text.split(self.sep))
    def join(self, words):
        "Return text from separate words."
        return self.sep.join(words)
    def getname(self, depth):
        "Return component field name for given depth."
        return self.names[depth]

class Document(object):
    """Delegated lucene Document.
    Provides mapping interface of field names to values, but duplicate field names are allowed.
    
    :param doc: optional lucene Document
    """
    def __init__(self, doc=None):
        self.doc = lucene.Document() if doc is None else doc
    def add(self, name, value, cls=Field, **params):
        "Add field to document with given parameters."
        for field in cls(name, **params).items(value):
            self.doc.add(field)
    def fields(self):
        "Generate lucene Fields."
        return itertools.imap(lucene.Field.cast_, self.doc.getFields())
    def __len__(self):
        return self.doc.getFields().size()
    def __contains__(self, name):
        return self.doc[name] is not None
    def __iter__(self):
        for field in self.fields():
            yield field.name()
    def items(self):
        "Generate name, value pairs for all fields."
        for field in self.fields():
            yield field.name(), field.stringValue()
    def __getitem__(self, name):
        value = self.doc[name]
        if value is None:
            raise KeyError(name)
        return value
    def get(self, name, default=None):
        "Return field value if present, else default."
        value = self.doc[name]
        return default if value is None else value
    def __delitem__(self, name):
        self.doc.removeFields(name)
    def getlist(self, name):
        "Return list of all values for given field."
        return list(self.doc.getValues(name))
    def dict(self, *names, **defaults):
        """Return dict representation of document.
        
        :param names: names of multi-valued fields to return as a list
        :param defaults: include only given fields, using default values as necessary
        """
        for name, value in defaults.items():
            defaults[name] = self.get(name, value)
        if not defaults:
            defaults = dict(self.items())
        defaults.update(zip(names, map(self.getlist, names)))
        return defaults

class Hit(Document):
    "A Document with an id and score, from a search result."
    def __init__(self, doc, id, score):
        Document.__init__(self, doc)
        self.id, self.score = id, score
    def dict(self, *names, **defaults):
        "Return dict representation of document with __id__ and __score__."
        result = Document.dict(self, *names, **defaults)
        result.update(__id__=self.id, __score__=self.score)
        return result

class Hits(object):
    """Search results: lazily evaluated and memory efficient.
    Provides a read-only sequence interface to hit objects.
    
    :param searcher: `IndexSearcher`_ which can retrieve documents
    :param ids: ordered doc ids
    :param scores: ordered doc scores
    :param count: total number of hits
    """
    def __init__(self, searcher, ids, scores, count=0):
        self.searcher = searcher
        self.ids, self.scores = ids, scores
        self.count = count or len(self)
    def __len__(self):
        return len(self.ids)
    def __getitem__(self, index):
        id, score = self.ids[index], self.scores[index]
        if isinstance(index, slice):
            return type(self)(self.searcher, id, score, self.count)
        return Hit(self.searcher.doc(id), id, score)
    def items(self):
        "Generate zipped ids and scores."
        return itertools.izip(self.ids, self.scores)
