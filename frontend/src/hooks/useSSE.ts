import { useCallback, useEffect, useRef } from 'react';
import type { SSEEvent } from '../types';

interface StreamOptions<TBody> {
  url: string;
  body: TBody;
  onEvent: (event: SSEEvent) => void;
  signal?: AbortSignal;
  retries?: number;
}

const parseEventBlock = (block: string): SSEEvent | null => {
  const lines = block
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);

  if (lines.length === 0) {
    return null;
  }

  let type: SSEEvent['type'] = 'token';
  const dataParts: string[] = [];

  for (const line of lines) {
    if (line.startsWith('event:')) {
      const value = line.slice(6).trim();
      if (value === 'token' || value === 'sources' || value === 'done' || value === 'error') {
        type = value;
      }
    }

    if (line.startsWith('data:')) {
      dataParts.push(line.slice(5).trim());
    }
  }

  const raw = dataParts.join('\n');
  let content: unknown = raw;

  if (raw.length > 0) {
    try {
      content = JSON.parse(raw);
    } catch {
      content = raw;
    }
  }

  if (typeof content === 'object' && content !== null && 'type' in content && 'content' in content) {
    const candidate = content as SSEEvent;
    return candidate;
  }

  return { type, content };
};

export const useSSE = () => {
  const activeControllerRef = useRef<AbortController | null>(null);

  const abort = useCallback(() => {
    activeControllerRef.current?.abort();
    activeControllerRef.current = null;
  }, []);

  const stream = useCallback(async <TBody,>({ url, body, onEvent, signal, retries = 1 }: StreamOptions<TBody>) => {
    let attempt = 0;

    while (attempt <= retries) {
      const controller = new AbortController();
      const forwardAbort = () => controller.abort();

      activeControllerRef.current = controller;
      signal?.addEventListener('abort', forwardAbort, { once: true });

      try {
        const response = await fetch(url, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Accept: 'text/event-stream',
          },
          body: JSON.stringify(body),
          signal: controller.signal,
        });

        if (!response.ok || !response.body) {
          throw new Error('A kapcsolat nem érhető el.');
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        while (true) {
          const { done, value } = await reader.read();

          if (done) {
            break;
          }

          buffer += decoder.decode(value, { stream: true });
          const blocks = buffer.split(/\r?\n\r?\n/);
          buffer = blocks.pop() ?? '';

          for (const block of blocks) {
            const event = parseEventBlock(block);
            if (event) {
              onEvent(event);
            }
          }
        }

        const trailingEvent = parseEventBlock(buffer);
        if (trailingEvent) {
          onEvent(trailingEvent);
        }

        return;
      } catch (error) {
        if (controller.signal.aborted) {
          throw error;
        }

        if (attempt === retries) {
          throw error instanceof Error ? error : new Error('Váratlan hálózati hiba történt.');
        }

        await new Promise((resolve) => window.setTimeout(resolve, 500 * (attempt + 1)));
        attempt += 1;
        continue;
      } finally {
        signal?.removeEventListener('abort', forwardAbort);
        if (activeControllerRef.current === controller) {
          activeControllerRef.current = null;
        }
      }
    }
  }, []);

  useEffect(() => abort, [abort]);

  return { abort, stream };
};
