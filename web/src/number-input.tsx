import { useLayoutEffect, useState } from 'react';
import type { InputHTMLAttributes } from 'react';

type Props = Omit<InputHTMLAttributes<HTMLInputElement>, 'type' | 'value' | 'onChange'> & {
  value: number | '';
  onValueChange: (value: number) => void;
};

// Keep the editable text separate from the numeric value submitted by the form.
// Empty and incomplete values retain native input validation without becoming zero.
export function NumberInput({ value, onValueChange, disabled, ...props }: Props) {
  const [text, setText] = useState(String(value));
  useLayoutEffect(() => {
    setText((current) =>
      !disabled && current !== '' && Number(current) === value ? current : String(value),
    );
  }, [value, disabled]);
  return (
    <input
      {...props}
      type="number"
      disabled={disabled}
      value={text}
      onChange={(event) => {
        setText(event.currentTarget.value);
        if (event.currentTarget.value !== '' && Number.isFinite(event.currentTarget.valueAsNumber))
          onValueChange(event.currentTarget.valueAsNumber);
      }}
    />
  );
}
