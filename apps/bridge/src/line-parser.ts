import { StringDecoder } from 'node:string_decoder';

/** Serial data is a byte stream, not a stream of whole JSON messages. */
export class BoundedLineParser {
  private readonly decoder = new StringDecoder('utf8');
  private text = '';
  private bytes = 0;
  private discarding = false;

  constructor(
    private readonly onLine: (line: string) => void,
    private readonly onOverflow: () => void,
    private readonly maxBytes = 8192,
  ) {}

  push(chunk: Uint8Array): void {
    // Scan bytes first so an unterminated message cannot grow the decoder buffer.
    let start = 0;
    for (let index = 0; index < chunk.length; index += 1) {
      if (chunk[index] === 10) {
        if (!this.discarding) {
          this.append(chunk.subarray(start, index));
          if (!this.discarding) {
            const tail = this.decoder.end();
            this.onLine((this.text + tail).replace(/\r$/, ''));
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
      this.decoder.end();
      this.onOverflow();
      return;
    }
    this.text += this.decoder.write(Buffer.from(chunk));
  }

  private reset(): void {
    this.text = '';
    this.bytes = 0;
    this.discarding = false;
    this.decoder.end();
  }
}
