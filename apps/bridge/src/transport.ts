import { SerialPort } from 'serialport';
import { createSerialCallbackWakeup } from './serial-compat.js';
import { SerialWriteQueue } from './serial-write-queue.js';

export interface PortDescription {
  path: string;
  manufacturer?: string | undefined;
  vendorId?: string | undefined;
  productId?: string | undefined;
  serialNumber?: string | undefined;
}

export interface TransportListeners {
  data: (chunk: Uint8Array) => void;
  error: (error: Error) => void;
  close: () => void;
}

export interface DeviceTransport {
  open(listeners: TransportListeners): Promise<void>;
  write(data: string): Promise<void>;
  close(): Promise<void>;
}

export function createSerialTransport(path: string): DeviceTransport {
  const port = new SerialPort({
    path,
    baudRate: 115200,
    autoOpen: false,
    dataBits: 8,
    stopBits: 1,
    parity: 'none',
    rtscts: false,
    xon: false,
    xoff: false,
  });
  const wakeup = createSerialCallbackWakeup();
  const writes = new SerialWriteQueue(
    (chunk) =>
      new Promise<void>((resolve, reject) => {
        port.write(chunk, (error) => (error ? reject(error) : resolve()));
      }),
  );
  let detach = () => {};
  // Serial errors must always have a listener, including during shutdown.
  port.on('error', () => {});
  return {
    async open(listeners) {
      const onData = (chunk: Buffer) => listeners.data(chunk);
      const onClose = () => {
        writes.close();
        wakeup.stop();
        listeners.close();
      };
      port.on('data', onData);
      port.on('error', listeners.error);
      port.on('close', onClose);
      detach = () => {
        port.off('data', onData);
        port.off('error', listeners.error);
        port.off('close', onClose);
      };
      // Cover open/set as well as read/write; stop only after native cleanup.
      wakeup.start();
      try {
        await new Promise<void>((resolve, reject) => {
          port.open((error) => (error ? reject(error) : resolve()));
        });
        await new Promise<void>((resolve, reject) => {
          port.set({ dtr: false, rts: false }, (error) => (error ? reject(error) : resolve()));
        });
        // Discard a partial frame left on the running device by an interrupted
        // previous connection before the controller requests its snapshot.
        await writes.write('\n');
      } catch (error) {
        writes.close();
        detach();
        try {
          if (port.isOpen) {
            await new Promise<void>((resolve) => port.close(() => resolve()));
          }
        } finally {
          wakeup.stop();
        }
        throw error;
      }
    },
    async write(data) {
      if (!port.isOpen) throw new Error('Serial port is not open');
      await writes.write(data);
    },
    async close() {
      writes.close();
      detach();
      try {
        if (!port.isOpen) return;
        await new Promise<void>((resolve, reject) => {
          port.close((error) => (error ? reject(error) : resolve()));
        });
      } finally {
        wakeup.stop();
      }
    },
  };
}

export async function listSerialPorts(): Promise<PortDescription[]> {
  return SerialPort.list();
}
