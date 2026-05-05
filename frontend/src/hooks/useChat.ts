import { useCallback, useMemo, useState } from 'react';
import { useSSE } from './useSSE';
import type { ChatMessage, ChatSource, SSEEvent } from '../types';

const createMessageId = (prefix: string) => `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

const getErrorMessage = (content: SSEEvent['content']) => {
  if (typeof content === 'string' && content.trim()) {
    return content;
  }

  if (typeof content === 'object' && content !== null && 'message' in content) {
    const message = (content as { message?: unknown }).message;
    if (typeof message === 'string' && message.trim()) {
      return message;
    }
  }

  return 'Nem sikerült választ kérni. Próbálja újra néhány másodperc múlva.';
};

export const useChat = () => {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { abort, stream } = useSSE();

  const updateAssistantMessage = useCallback((assistantId: string, updater: (message: ChatMessage) => ChatMessage) => {
    setMessages((current) => current.map((message) => (message.id === assistantId ? updater(message) : message)));
  }, []);

  const sendMessage = useCallback(
    async (input: string) => {
      const content = input.trim();
      if (!content || isLoading) {
        return;
      }

      setError(null);
      setIsLoading(true);
      abort();

      const userMessage: ChatMessage = {
        id: createMessageId('user'),
        role: 'user',
        content,
      };

      const assistantId = createMessageId('assistant');
      const assistantMessage: ChatMessage = {
        id: assistantId,
        role: 'assistant',
        content: '',
        isStreaming: true,
      };

      const nextMessages = [...messages, userMessage];
      setMessages((current) => [...current, userMessage, assistantMessage]);

      try {
        const apiBase = import.meta.env.VITE_API_BASE_URL || '';
        await stream({
          url: `${apiBase}/api/chat`,
          body: {
            message: content,
            history: nextMessages.map(({ content: messageContent, role, sources }) => ({
              content: messageContent,
              role,
              sources,
            })),
          },
          onEvent: (event) => {
            if (event.type === 'token') {
              const token = typeof event.content === 'string' ? event.content : '';
              updateAssistantMessage(assistantId, (message) => ({
                ...message,
                content: `${message.content}${token}`,
                isStreaming: true,
              }));
              return;
            }

            if (event.type === 'sources') {
              const sources = Array.isArray(event.content) ? (event.content as ChatSource[]) : [];
              updateAssistantMessage(assistantId, (message) => ({
                ...message,
                sources,
              }));
              return;
            }

            if (event.type === 'error') {
              const message = getErrorMessage(event.content);
              setError(message);
              updateAssistantMessage(assistantId, (currentMessage) => ({
                ...currentMessage,
                content: currentMessage.content || message,
                isStreaming: false,
              }));
              return;
            }

            if (event.type === 'done') {
              updateAssistantMessage(assistantId, (message) => ({
                ...message,
                isStreaming: false,
              }));
            }
          },
        });
      } catch (streamError) {
        const message = streamError instanceof Error ? streamError.message : 'Váratlan hiba történt.';
        setError(message);
        updateAssistantMessage(assistantId, (currentMessage) => ({
          ...currentMessage,
          content: currentMessage.content || message,
          isStreaming: false,
        }));
      } finally {
        setIsLoading(false);
        updateAssistantMessage(assistantId, (message) => ({
          ...message,
          isStreaming: false,
        }));
      }
    },
    [abort, isLoading, messages, stream, updateAssistantMessage],
  );

  const value = useMemo(
    () => ({
      messages,
      isLoading,
      error,
      sendMessage,
    }),
    [error, isLoading, messages, sendMessage],
  );

  return value;
};
