const TypingIndicator = () => {
  return (
    <div aria-label="A válasz készül" className="typing-indicator" role="status">
      <span className="typing-indicator__dot" />
      <span className="typing-indicator__dot" />
      <span className="typing-indicator__dot" />
    </div>
  );
};

export default TypingIndicator;
