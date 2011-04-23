"""
Custom server.

Fields settings are assigned directly to the root.
Indexing is done here just to populate the example.

A custom filter and sorter are demonstrated by transforming a date field into a year field.
Filters are also used for faceting;  sorters are also used for grouping.

Example queries:
 * http://localhost:8080/search?q=date:17*&group=year
 * http://localhost:8080/search?q=date:17*&group=year&sort=-year
 * http://localhost:8080/search?count=0&facets=year
 * http://localhost:8080/search?q=text:right&count=3&facets=year
"""

import os
import lucene
from lupyne import engine, server
from test import fixture

if __name__ == '__main__':
    lucene.initVM(vmargs='-Xrs')
    root = server.WebIndexer()
    # assign field settings
    root.indexer.set('amendment', store=True, index=True)
    root.indexer.set('date', store=True, index=True)
    root.indexer.set('text')
    # populate index
    for doc in fixture.constitution.docs():
        if 'amendment' in doc:
            root.indexer.add(doc)
    root.update()
    # assign custom filter and sorter based on year
    root.searcher.sorters['year'] = engine.SortField('date', int, lambda date: int(date.split('-')[0]))
    years = set(date.split('-')[0] for date in root.searcher.terms('date'))
    root.searcher.filters['year'] = dict((year, engine.Query.prefix('date', year).filter()) for year in years)
    # start with pretty-printing
    server.start(root, config={'global': {'tools.json.indent': 2}})
