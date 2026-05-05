import { render, screen } from '@testing-library/react';
import { vi } from 'vitest';
import ChatWindow from '../ChatWindow';
import type { ChatMessage } from '../../types';

describe('ChatWindow', () => {
  it('renders empty state suggestions', () => {
    render(<ChatWindow messages={[]} />);

    expect(screen.getByText('Kérdezzen a NAV tájékoztatók alapján')).toBeInTheDocument();
    expect(screen.getByText('Mikor kell áfabevallást benyújtani egyéni vállalkozóként?')).toBeInTheDocument();
  });

  it('renders messages', () => {
    const messages: ChatMessage[] = [
      { id: '1', role: 'user', content: 'Mikor esedékes az áfabevallás?' },
      { id: '2', role: 'assistant', content: 'Az esedékesség az adózási gyakoriságtól függ.' },
    ];

    render(<ChatWindow messages={messages} />);

    expect(screen.getByText('Mikor esedékes az áfabevallás?')).toBeInTheDocument();
    expect(screen.getByText('Az esedékesség az adózási gyakoriságtól függ.')).toBeInTheDocument();
  });

  it('auto-scrolls when messages change', () => {
    const scrollIntoView = vi.fn();
    Object.defineProperty(window.HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      value: scrollIntoView,
    });

    const { rerender } = render(<ChatWindow messages={[]} />);
    expect(scrollIntoView).toHaveBeenCalledTimes(1);

    rerender(
      <ChatWindow
        messages={[{ id: '1', role: 'assistant', content: 'Friss válasz érkezett.' }]}
        isLoading
      />,
    );

    expect(scrollIntoView).toHaveBeenCalledTimes(2);
  });
});
