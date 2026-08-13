import { useRef, useState, type FormEvent, type KeyboardEvent } from 'react';

import './composer.css';

interface Props {
  onSend: (content: string) => void;
  onCancel: () => void;
  disabled: boolean;
}

const MAX_TEXTAREA_HEIGHT_PX = 200;

export function Composer({ onSend, onCancel, disabled }: Props) {
  const [value, setValue] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const resize = () => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, MAX_TEXTAREA_HEIGHT_PX)}px`;
  };

  const submit = () => {
    const trimmed = value.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setValue('');
    requestAnimationFrame(resize);
  };

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    submit();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter sends, Shift+Enter inserts a newline.
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  };

  return (
    <form className="composer" onSubmit={handleSubmit}>
      <div className="composer__field">
        <label className="composer__label" htmlFor="composer-input">
          Message
        </label>
        <textarea
          id="composer-input"
          ref={textareaRef}
          className="composer__input"
          value={value}
          rows={1}
          placeholder="Send a message…"
          onChange={(event) => {
            setValue(event.target.value);
            resize();
          }}
          onKeyDown={handleKeyDown}
        />
        {disabled ? (
          <button
            className="composer__cancel"
            type="button"
            onClick={onCancel}
            data-testid="cancel-button"
          >
            Stop
          </button>
        ) : (
          <button className="composer__send" type="submit" disabled={!value.trim()}>
            Send
          </button>
        )}
      </div>
      <p className="composer__hint">Enter to send · Shift + Enter for a new line</p>
    </form>
  );
}
