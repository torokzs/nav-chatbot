import { renderHook, act, waitFor } from '@testing-library/react';
import { vi } from 'vitest';
import { useChat } from '../useChat';

const createSSEBody = (chunks: string[]) => {
  const encoder = new TextEncoder();

  return new ReadableStream({
    start(controller) {
      chunks.forEach((chunk) => controller.enqueue(encoder.encode(chunk)));
      controller.close();
    },
  });
};

describe('useChat', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('returns initial state', () => {
    const { result } = renderHook(() => useChat());

    expect(result.current.messages).toEqual([]);
    expect(result.current.isLoading).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it('sendMessage updates messages from SSE response', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        createSSEBody([
          'event: token\ndata: "Ez egy teszt válasz."\n\n',
          'event: sources\ndata: [{"fuzet_szam":"7","fuzet_cim":"Adózás rendje","page_from":10,"page_to":12,"breadcrumb":"Eljárás","url":"/api/documents/7/pdf"}]\n\n',
          'event: done\ndata: true\n\n',
        ]),
        { status: 200 },
      ),
    );

    vi.stubGlobal('fetch', fetchMock);

    const { result } = renderHook(() => useChat());

    await act(async () => {
      await result.current.sendMessage('Teszt kérdés');
    });

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(result.current.messages).toHaveLength(2);
    expect(result.current.messages[0]).toMatchObject({ role: 'user', content: 'Teszt kérdés' });
    expect(result.current.messages[1]).toMatchObject({
      role: 'assistant',
      content: 'Ez egy teszt válasz.',
      isStreaming: false,
    });
    expect(result.current.messages[1].sources).toEqual([
      {
        fuzet_szam: '7',
        fuzet_cim: 'Adózás rendje',
        page_from: 10,
        page_to: 12,
        breadcrumb: 'Eljárás',
        url: '/api/documents/7/pdf',
      },
    ]);
  });
});
