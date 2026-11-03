import test from 'node:test';
import assert from 'node:assert/strict';
import { PreviewQueue } from '../entropy_arb/webui/v2/grid/preview_queue.js';

function harness(run) {
  let next = 0, visible = true;
  const timers = new Map();
  const queue = new PreviewQueue(run, {
    visible: () => visible,
    schedule: fn => { timers.set(++next, fn); return next; },
    cancel: id => timers.delete(id),
  });
  return { queue, timers, hide: () => visible = false, show: () => visible = true,
    tick: () => {
      const [id, fn] = timers.entries().next().value;
      timers.delete(id);
      return fn();
    },
  };
}

test('rapid edits replace pending timers and only the latest draft runs', async () => {
  let draft = 'first'; const seen = [];
  const h = harness(async () => seen.push(draft));
  h.queue.request(); draft = 'second'; h.queue.request();
  draft = 'latest'; h.queue.request();
  assert.equal(h.timers.size, 1);
  await h.tick();
  assert.deepEqual(seen, ['latest']);
  assert.equal(h.timers.size, 0); // no recurring work after completion
});

test('an in-flight preview cannot overlap and one latest follow-up survives', async () => {
  let draft = 'first', release; const seen = [];
  const h = harness(async () => {
    seen.push(draft);
    if (seen.length === 1) await new Promise(resolve => release = resolve);
  });
  h.queue.request(); const first = h.tick();
  draft = 'second'; h.queue.request(); draft = 'latest'; h.queue.request();
  assert.deepEqual(seen, ['first']); assert.equal(h.timers.size, 0);
  release(); await first;
  assert.equal(h.timers.size, 1);
  await h.tick(); assert.deepEqual(seen, ['first', 'latest']);
});

test('hidden page defers work and a destroyed page never starts it', async () => {
  let calls = 0; const h = harness(async () => calls++);
  h.hide(); h.queue.request(); assert.equal(h.timers.size, 0);
  h.show(); h.queue.foreground(); assert.equal(h.timers.size, 1);
  h.hide(); await h.tick(); assert.equal(calls, 0);
  h.show(); h.queue.foreground(); await h.tick(); assert.equal(calls, 1);
  h.queue.request(); h.queue.close(); assert.equal(h.timers.size, 0);
  h.queue.request(); h.queue.foreground(); await h.queue.flush();
  assert.equal(calls, 1);
});

test('destroying during a request drops queued work; a failed request releases its slot', async () => {
  let release, calls = 0;
  const h = harness(async () => { calls++; await new Promise(r => release = r); });
  h.queue.request(); const first = h.tick(); h.queue.request(); h.queue.close();
  release(); await first; assert.equal(calls, 1); assert.equal(h.timers.size, 0);
  const failure = harness(async () => { throw new Error('preview unavailable'); });
  failure.queue.request(); await assert.rejects(failure.tick(), /preview unavailable/);
  assert.equal(failure.queue.running, false);
  failure.queue.request(); assert.equal(failure.timers.size, 1); failure.queue.close();
});

test('native timer functions do not receive the queue as their this value', async () => {
  const originalSet = globalThis.setTimeout, originalClear = globalThis.clearTimeout;
  let callback;
  try {
    globalThis.setTimeout = function(fn) {
      assert.ok(!(this instanceof PreviewQueue)); callback = fn; return 1;
    };
    globalThis.clearTimeout = function() { assert.ok(!(this instanceof PreviewQueue)); };
    let calls = 0;
    const q = new PreviewQueue(async () => calls++);
    q.request(); await callback(); q.close();
    assert.equal(calls, 1);
  } finally {
    globalThis.setTimeout = originalSet; globalThis.clearTimeout = originalClear;
  }
});
