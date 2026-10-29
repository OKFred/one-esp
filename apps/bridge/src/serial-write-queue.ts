/** UART0's small receive ring can overflow while MicroPython writes an OLED
 * snapshot. Pace bytes and serialize complete lines across all HTTP callers. */
export class SerialWriteQueue {
  private readonly pending: {
    buffer: Buffer;
    resolve(): void;
    reject(error: Error): void;
  }[] = [];
  private active: (typeof this.pending)[number] | undefined;
  private closed = false;
  private pause: { timer: ReturnType<typeof setTimeout>; reject(error: Error): void } | undefined;

  constructor(private readonly writeChunk: (chunk: Buffer) => Promise<void>) {}

  write(data: string): Promise<void> {
    if (this.closed) return Promise.reject(new Error('Serial session is closed'));
    return new Promise<void>((resolve, reject) => {
      this.pending.push({ buffer: Buffer.from(data, 'utf8'), resolve, reject });
      this.pump();
    });
  }

  close(error = new Error('Serial session is closed')): void {
    if (this.closed) return;
    this.closed = true;
    this.active?.reject(error);
    for (const job of this.pending.splice(0)) job.reject(error);
    if (this.pause) {
      clearTimeout(this.pause.timer);
      this.pause.reject(error);
      this.pause = undefined;
    }
  }

  private assertOpen(): void {
    if (this.closed) throw new Error('Serial session is closed');
  }

  private wait(): Promise<void> {
    return new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pause = undefined;
        resolve();
      }, 60);
      this.pause = { timer, reject };
    });
  }

  private pump(): void {
    if (this.closed || this.active) return;
    const job = this.pending.shift();
    if (!job) return;
    this.active = job;
    void (async () => {
      try {
        for (let offset = 0; offset < job.buffer.length; offset += 64) {
          this.assertOpen();
          await this.writeChunk(job.buffer.subarray(offset, offset + 64));
          this.assertOpen();
          if (offset + 64 < job.buffer.length) await this.wait();
        }
        job.resolve();
        // Keep the queue occupied during the final gap, but bytes already handed
        // to the driver remain accepted if the caller disconnects in this gap.
        await this.wait();
      } catch (error) {
        // A partially transmitted line must never be followed by another frame.
        this.close(error instanceof Error ? error : new Error('Serial write failed'));
      } finally {
        this.active = undefined;
        this.pump();
      }
    })();
  }
}
