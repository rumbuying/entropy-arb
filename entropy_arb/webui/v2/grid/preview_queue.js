/* A page has at most one preview in flight. Edits coalesce into one
   latest follow-up; hidden/destroyed pages cannot schedule more work. */
export class PreviewQueue {
  constructor(run, { visible = () => true, schedule = setTimeout,
    cancel = clearTimeout, delay = 350 } = {}) {
    this.run = run;
    this.visible = visible;
    // Window timer APIs must not receive this queue as their receiver.
    this.schedule = (fn, delay) => schedule(fn, delay);
    this.cancel = id => cancel(id);
    this.delay = delay;
    this.timer = null;
    this.running = false;
    this.pending = false;
    this.closed = false;
  }
  request(delay = this.delay) {
    if (this.closed) return;
    this.pending = true;
    this.cancel(this.timer);
    this.timer = null;
    if (!this.running && this.visible())
      this.timer = this.schedule(() => this.flush(), delay);
  }
  async flush() {
    this.timer = null;
    if (this.closed || this.running || !this.pending || !this.visible()) return;
    this.pending = false;
    this.running = true;
    try {
      await this.run();
    } finally {
      this.running = false;
      if (this.pending && !this.closed) this.request(0);
    }
  }
  foreground() {
    if (this.pending && !this.running && this.visible()) this.request(0);
  }
  close() {
    this.closed = true;
    this.pending = false;
    this.cancel(this.timer);
    this.timer = null;
  }
}
