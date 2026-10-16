"""Pagination: το continuationToken έρχεται στο invoice/v1.0 namespace → πρέπει να φέρνει ΟΛΕΣ τις σελίδες
(python -m tests.test_pagination)."""
from types import SimpleNamespace as NS

from mydata_client import MyDataClient

X = 'xmlns="http://www.aade.gr/myDATA/invoice/v1.0"'
PAGE1 = (f'<RequestedDoc {X}><continuationToken><nextPartitionKey>p</nextPartitionKey>'
         f'<nextRowKey>r</nextRowKey></continuationToken>'
         f'<invoicesDoc><invoice><mark>1</mark></invoice></invoicesDoc></RequestedDoc>').encode()
PAGE2 = f'<RequestedDoc {X}><invoicesDoc><invoice><mark>2</mark></invoice></invoicesDoc></RequestedDoc>'.encode()

c = MyDataClient("u", "k", "dev")
calls = []
c.session.get = lambda url, params, **k: calls.append(dict(params)) or NS(
    status_code=200, content=PAGE2 if "nextPartitionKey" in params else PAGE1)
top, _, _ = c.new_since("RequestDocs", 0)
assert len(calls) == 2 and calls[1]["nextPartitionKey"] == "p" and calls[1]["nextRowKey"] == "r", calls
assert top == 2, top
print("ok")
