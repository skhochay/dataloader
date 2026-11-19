/** A labelled form row with optional help text and an error message, wired for screen readers. */
export default function FormRow({ id, label, help, error, children }) {
  const describedBy = [help && `${id}-help`, error && `${id}-error`].filter(Boolean).join(" ") || undefined;
  return (
    <div className={`field ${error ? "field-error" : ""}`}>
      <label className="field-label" htmlFor={id}>{label}</label>
      <div className="field-body" aria-describedby={describedBy}>
        {children}
        {help && <p className="field-help" id={`${id}-help`}>{help}</p>}
        {error && <p className="field-msg" id={`${id}-error`} role="alert">{error}</p>}
      </div>
    </div>
  );
}
