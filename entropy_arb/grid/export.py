"""Bounded-memory audit archives from one read-only SQLite snapshot.

No venue or vault access. Temporary archives are private, expire, and are
read in offset-addressed chunks so a repeated read cannot skip bytes.
"""
from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import io
import json
import re
import tempfile
import time
import uuid
import zipfile

from . import models, store


class ExportError(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


QUERIES = {
    'orders': "SELECT i.*,o.cumulative_qty,o.exchange_order_id,o.terminal_state "
              "FROM order_intents i LEFT JOIN exchange_orders o ON o.intent_id=i.intent_id "
              "WHERE i.grid_id=? ORDER BY i.created_ts,i.intent_id",
    'exchange_orders': "SELECT o.* FROM exchange_orders o JOIN order_intents i USING(intent_id) "
                       "WHERE i.grid_id=? ORDER BY o.updated_ts,o.client_id",
    'fills': "SELECT e.* FROM executions e JOIN execution_owners o USING(execution_key) "
             "WHERE o.grid_id=? ORDER BY e.exchange_ts,e.execution_key",
    'pairs': "SELECT * FROM pair_allocations WHERE grid_id=? ORDER BY pair_id",
    'events': "SELECT * FROM events WHERE scope=? ORDER BY seq",
    'funding': "SELECT f.* FROM funding_payments f JOIN funding_owners o USING(payment_key) "
               "WHERE o.grid_id=? ORDER BY f.ts,f.payment_key",
    'capital': "SELECT * FROM capital_movements WHERE grid_id=? ORDER BY ts,movement_id",
    'versions': "SELECT * FROM grid_versions WHERE grid_id=? ORDER BY revision",
    'levels': "SELECT * FROM grid_levels WHERE grid_id=? ORDER BY revision,level_index",
    'inventory': "SELECT * FROM inventory_lots WHERE grid_id=? ORDER BY open_ts,lot_id",
    'operations': "SELECT * FROM operations WHERE grid_id=? ORDER BY created_ts,operation_id",
    'dispatch_attempts': "SELECT d.* FROM dispatch_attempts d JOIN order_intents i USING(intent_id) "
                         "WHERE i.grid_id=? ORDER BY d.attempt_id",
    'protection': "SELECT r.key,r.value FROM runtime_state r JOIN order_intents i "
                  "ON r.key='order-protection:'||i.intent_id WHERE i.grid_id=? ORDER BY r.key",
    'observations': "WITH target AS (SELECT ? AS gid) SELECT r.key,r.value FROM runtime_state r,target "
                    "WHERE r.key IN ('range-watch:'||target.gid,'risk-watch:'||target.gid,"
                    "'protection-exit:'||target.gid,'coverage:'||target.gid,'history-start:'||target.gid) ORDER BY r.key",
}


def csv_cell(value):
    text = '' if value is None else str(value)
    # Preserve ordinary signed decimal amounts, neutralize spreadsheet formulas
    # even after leading whitespace/control characters. csv quoting alone does
    # not prevent a spreadsheet from executing a formula.
    if text.lstrip().startswith(('=', '+', '-', '@')) and not re.fullmatch(
            r'-?\d+(?:\.\d+)?', text):
        return "'" + text
    return text


def build_archive(db_path, grid_id):
    if db_path == ':memory:':
        raise ExportError('EXPORT_UNAVAILABLE', 'audit exports require a persistent database', 422)
    output = tempfile.TemporaryFile(mode='w+b')
    conn = None
    try:
        conn = store.connect(db_path, readonly=True)
        conn.execute('BEGIN')
        grid = conn.execute('SELECT * FROM grids WHERE grid_id=?', (grid_id,)).fetchone()
        if grid is None:
            raise ExportError('NOT_FOUND', 'grid not found', 404)
        manifest = {'format_version': 1, 'grid': dict(grid),
                    'snapshot_ts': models.utc_now_iso(),
                    'event_high_water': conn.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0],
                    'source': 'local-committed-ledger', 'files': {},
                    'notes': ['One SQLite read transaction; later updates are excluded.',
                              'No private keys or credential vault are read.',
                              'Decimal values and native identifiers are textual; import CSV columns as text.',
                              'Formula-like text cells are prefixed with an apostrophe.',
                              'Snapshot completeness does not certify exchange history coverage.']}
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, query in QUERIES.items():
                cursor = conn.execute(query, (grid_id,))
                columns = [d[0] for d in cursor.description]
                digest = hashlib.sha256()
                count = 0
                with archive.open(name + '.csv', 'w', force_zip64=True) as member:
                    # Encode one row at a time, never materialize the full ledger.
                    line = io.StringIO(newline='')
                    writer = csv.writer(line, lineterminator='\r\n')
                    def emit(values, bom=False):
                        line.seek(0); line.truncate(0)
                        writer.writerow([csv_cell(v) for v in values])
                        data = (('\ufeff' if bom else '') + line.getvalue()).encode('utf-8')
                        digest.update(data); member.write(data)
                    emit(columns, bom=True)
                    for row in cursor:
                        emit(row); count += 1
                manifest['files'][name + '.csv'] = {'rows': count, 'sha256': digest.hexdigest()}
            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        conn.execute('ROLLBACK')
        output.seek(0, 2)
        size = output.tell(); output.seek(0)
        return output, size, manifest
    except BaseException:
        output.close()
        raise
    finally:
        if conn is not None:
            conn.close()


class ExportManager:
    def __init__(self, db_path, *, clock=time.monotonic, ttl=300, capacity=2):
        self.db_path, self.clock, self.ttl, self.capacity = db_path, clock, ttl, capacity
        self.exports, self.building = {}, 0
        self.closed = False
        self.workers = set()

    def prune(self):
        for key, item in list(self.exports.items()):
            if self.clock() >= item['expires']:
                item['file'].close(); del self.exports[key]

    async def begin(self, grid_id):
        self.prune()
        if self.closed or self.building + len(self.exports) >= self.capacity:
            raise ExportError('EXPORT_BUSY', 'another export is in progress; try again later', 429)
        self.building += 1
        task = asyncio.create_task(asyncio.to_thread(build_archive, self.db_path, grid_id))
        self.workers.add(task)
        def finished(done):
            self.workers.discard(done)
        task.add_done_callback(finished)
        try:
            try:
                file, size, manifest = await asyncio.shield(task)
            except asyncio.CancelledError:
                # A worker thread cannot be cancelled. Close its eventual output
                # rather than leaking a private archive after disconnect/shutdown.
                def cleanup(done):
                    if not done.cancelled() and done.exception() is None:
                        done.result()[0].close()
                task.add_done_callback(cleanup)
                raise
            if self.closed:
                file.close()
                raise ExportError('EXPORT_UNAVAILABLE', 'runtime is stopping', 503)
            key = uuid.uuid4().hex
            self.exports[key] = {'file': file, 'size': size, 'expires': self.clock() + self.ttl}
            return {'export_id': key, 'size': size, 'manifest': manifest}
        finally:
            # Keep the slot until publication or the cancelled worker really
            # finishes. A disconnected client must not create unbounded threads.
            if task.done():
                self.building -= 1
            else:
                task.add_done_callback(lambda done: setattr(self, 'building', self.building - 1))

    async def reap(self):
        while not self.closed:
            await asyncio.sleep(min(30, self.ttl))
            self.prune()

    def chunk(self, key, offset):
        self.prune()
        item = self.exports.get(key)
        if item is None:
            raise ExportError('EXPORT_EXPIRED', 'export expired; start a new download', 410)
        if type(offset) is not int or not 0 <= offset <= item['size']:
            raise ExportError('VALIDATION_ERROR', 'invalid export offset')
        item['file'].seek(offset)
        data = item['file'].read(32 * 1024)
        item['expires'] = self.clock() + self.ttl
        return {'offset': offset, 'next_offset': offset + len(data),
                'data': base64.b64encode(data).decode('ascii'),
                'done': offset + len(data) == item['size']}

    def release(self, key):
        item = self.exports.pop(key, None)
        if item is not None:
            item['file'].close()

    def close(self):
        self.closed = True
        for key in list(self.exports):
            self.release(key)
