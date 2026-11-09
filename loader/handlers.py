"""Where parsed rows go. Swap this out to write into your own tables."""
from .models import Record
from .parsers import ParseError


def load_records(job, rows, batch_size=1000):
    """Bulk-insert rows for `job` and return the count.

    The engine calls this inside one transaction, so a failure part-way
    through leaves nothing behind.
    """
    total = 0
    batch = []
    for row_number, data in rows:
        batch.append(Record(job=job, row_number=row_number, data=data))
        if len(batch) >= batch_size:
            Record.objects.bulk_create(batch)
            total += len(batch)
            batch = []
    if batch:
        Record.objects.bulk_create(batch)
        total += len(batch)
    if total == 0:
        raise ParseError("file contains no data rows")
    return total
