import { useState } from "react";
import { saveToken } from "../token";

export function TokenField({ token, onChange }: { token: string; onChange: (t: string) => void }) {
  const [draft, setDraft] = useState("");
  if (token) {
    return (
      <div className="token">
        <span>Curator mode</span>
        <button
          type="button"
          className="link"
          onClick={() => {
            saveToken("");
            onChange("");
          }}
        >
          Sign out
        </button>
      </div>
    );
  }
  return (
    <form
      className="token"
      onSubmit={(e) => {
        e.preventDefault();
        const value = draft.trim();
        if (!value) return;
        saveToken(value);
        onChange(value);
        setDraft("");
      }}
    >
      <label>
        Curator token{" "}
        <input
          type="password"
          autoComplete="off"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
        />
      </label>
      <button type="submit">Use</button>
    </form>
  );
}
