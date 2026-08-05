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
          'event: sources\ndata: [{"adoev":2026,"fuzet_szam":"7","fuzet_cim":"Adózás rendje","page_from":10,"page_to":12,"breadcrumb":"Eljárás","url":"/api/documents/2026/7/pdf"}]\n\n',
          'event: done\ndata: true\n\n',
        ]),
        { status: 200 },
      ),
    );

    vi.stubGlobal('fetch', fetchMock);

    const { result } = renderHook(() => useChat());

    await act(async () => {
      await result.current.sendMessage('Teszt kérdés 2026-ra');
    });

    await waitFor(() => expect(result.current.isLoading).toBe(false));

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(result.current.messages).toHaveLength(2);
    expect(result.current.messages[0]).toMatchObject({ role: 'user', content: 'Teszt kérdés 2026-ra', adoev: 2026 });
    expect(result.current.messages[1]).toMatchObject({
      role: 'assistant',
      content: 'Ez egy teszt válasz.',
      adoev: 2026,
      isStreaming: false,
    });
    expect(result.current.messages[1].sources).toEqual([
      {
        adoev: 2026,
        fuzet_szam: '7',
        fuzet_cim: 'Adózás rendje',
        page_from: 10,
        page_to: 12,
        breadcrumb: 'Eljárás',
        url: '/api/documents/2026/7/pdf',
      },
    ]);
  });

  it('asks for a tax year before calling the API', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useChat());

    await act(async () => {
      await result.current.sendMessage('Mikor kell bevallani?');
    });

    expect(fetchMock).not.toHaveBeenCalled();
    expect(result.current.awaitingTaxYear).toBe(true);
  });

  it('uses an explicit question year over the selected year', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(createSSEBody(['event: token\ndata: "Válasz"\n\n', 'event: done\ndata: true\n\n']), {
        status: 200,
      }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const { result } = renderHook(() => useChat());

    act(() => result.current.setSelectedTaxYear(2022));
    await act(async () => {
      await result.current.sendMessage('Mi változott 2025-ben?');
    });

    expect(result.current.selectedTaxYear).toBe(2025);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toMatchObject({ adoev: 2025 });
  });
});
