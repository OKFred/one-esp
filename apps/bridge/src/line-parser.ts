/** Serial data is a byte stream, not a stream of whole JSON messages. */
export class BoundedLineParser {
  private decoder = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true });
  private text = '';
  private bytes = 0;
  private discarding = false;

  constructor(
    private readonly onLine: (line: string) => void,
    private readonly onOverflow: () => void,
    private readonly maxBytes = 8192,
    private readonly onInvalidUtf8: () => void = () => {},
  ) {}

  push(chunk: Uint8Array): void {
    // Scan bytes first so an unterminated message cannot grow the decoder buffer.
    let start = 0;
    for (let index = 0; index < chunk.length; index += 1) {
      if (chunk[index] === 10) {
        if (!this.discarding) {
          this.append(chunk.subarray(start, index));
          if (!this.discarding) {
            let tail = '';
            try {
              tail = this.decoder.decode();
            } catch {
              this.rejectUtf8();
            }
            if (!this.discarding) this.onLine((this.text + tail).replace(/\r$/, ''));
          }
        }
        this.reset();
        start = index + 1;
      }
    }
    this.append(chunk.subarray(start));
  }

  private append(chunk: Uint8Array): void {
    if (this.discarding || chunk.length === 0) return;
    this.bytes += chunk.length;
    if (this.bytes > this.maxBytes) {
      this.discarding = true;
      this.text = '';
      this.onOverflow();
      return;
    }
    try {
      this.text += this.decoder.decode(chunk, { stream: true });
    } catch {
      this.rejectUtf8();
    }
  }

  private rejectUtf8(): void {
    this.discarding = true;
    this.text = '';
    this.onInvalidUtf8();
  }

  private reset(): void {
    this.text = '';
    this.bytes = 0;
    this.discarding = false;
    this.decoder = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true });
  }
}
