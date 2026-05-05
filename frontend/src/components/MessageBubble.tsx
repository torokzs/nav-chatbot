import type { CSSProperties, ReactNode } from 'react';
import type { ChatMessage } from '../types';
import { colors, spacing } from '../styles/theme';
import SourceCard from './SourceCard';
import TypingIndicator from './TypingIndicator';

interface MessageBubbleProps {
  message: ChatMessage;
}

const renderInline = (text: string) => {
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, index) => {
    if (part.startsWith('**') && part.endsWith('**')) {
      return <strong key={`${part}-${index}`}>{part.slice(2, -2)}</strong>;
    }

    return <span key={`${part}-${index}`}>{part}</span>;
  });
};

const renderRichText = (content: string) => {
  const lines = content.split(/\r?\n/);
  const elements: ReactNode[] = [];
  let paragraphBuffer: string[] = [];
  let listItems: string[] = [];
  let listType: 'ul' | 'ol' | null = null;

  const flushParagraph = () => {
    if (paragraphBuffer.length === 0) {
      return;
    }

    const text = paragraphBuffer.join(' ').trim();
    if (text) {
      elements.push(
        <p key={`paragraph-${elements.length}`} style={{ margin: 0 }}>
          {renderInline(text)}
        </p>,
      );
    }
    paragraphBuffer = [];
  };

  const flushList = () => {
    if (!listType || listItems.length === 0) {
      return;
    }

    const items = listItems.map((item, index) => <li key={`${item}-${index}`}>{renderInline(item)}</li>);
    elements.push(
      listType === 'ul' ? (
        <ul key={`list-${elements.length}`} style={{ margin: 0, paddingLeft: '1.25rem' }}>
          {items}
        </ul>
      ) : (
        <ol key={`list-${elements.length}`} style={{ margin: 0, paddingLeft: '1.25rem' }}>
          {items}
        </ol>
      ),
    );

    listItems = [];
    listType = null;
  };

  lines.forEach((rawLine) => {
    const line = rawLine.trim();

    if (!line) {
      flushParagraph();
      flushList();
      return;
    }

    const unorderedMatch = /^[-*]\s+(.+)$/.exec(line);
    const orderedMatch = /^\d+\.\s+(.+)$/.exec(line);

    if (unorderedMatch || orderedMatch) {
      flushParagraph();
      const nextType = unorderedMatch ? 'ul' : 'ol';
      const itemText = (unorderedMatch ?? orderedMatch)?.[1] ?? '';

      if (listType && listType !== nextType) {
        flushList();
      }

      listType = nextType;
      listItems.push(itemText);
      return;
    }

    flushList();
    paragraphBuffer.push(line);
  });

  flushParagraph();
  flushList();

  return elements.length > 0 ? elements : <p style={{ margin: 0 }}>{content}</p>;
};

const MessageBubble = ({ message }: MessageBubbleProps) => {
  const isUser = message.role === 'user';
  const bubbleStyles = {
    maxWidth: 'min(100%, 46rem)',
    alignSelf: isUser ? 'flex-end' : 'flex-start',
    backgroundColor: isUser ? colors.secondary : colors.white,
    color: isUser ? colors.white : colors.text,
    borderRadius: isUser ? '1.25rem 1.25rem 0.375rem 1.25rem' : '1.25rem 1.25rem 1.25rem 0.375rem',
    border: isUser ? 'none' : `1px solid ${colors.border}`,
    boxShadow: isUser ? '0 10px 24px rgba(0, 102, 204, 0.22)' : '0 8px 18px rgba(0, 51, 102, 0.08)',
    padding: spacing[4],
    display: 'grid',
    gap: spacing[3],
  } satisfies CSSProperties;

  return (
    <article aria-busy={message.isStreaming} style={bubbleStyles}>
      <div style={{ display: 'grid', gap: spacing[2] }}>
        {message.content ? renderRichText(message.content) : null}
        {message.isStreaming ? <TypingIndicator /> : null}
      </div>
      {!isUser && message.sources && message.sources.length > 0 ? (
        <div aria-label="Források" style={{ display: 'flex', flexWrap: 'wrap', gap: spacing[2] }}>
          {message.sources.slice(0, 3).map((source, idx) => (
            <SourceCard
              key={`${source.fuzet_szam}-${source.page_from}-${source.page_to}-${source.breadcrumb}`}
              source={source}
              index={idx + 1}
            />
          ))}
        </div>
      ) : null}
    </article>
  );
};

export default MessageBubble;
