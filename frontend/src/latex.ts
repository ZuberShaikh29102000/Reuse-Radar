// Readable text for LaTeX in paper *titles* (INSPIRE stores them with math markup).
// Evidence quotes are never passed through this: they must stay verbatim.

const SYMBOLS: Record<string, string> = {
  alpha: "α", beta: "β", gamma: "γ", delta: "δ", epsilon: "ε", eta: "η", theta: "θ",
  kappa: "κ", lambda: "λ", mu: "μ", nu: "ν", pi: "π", rho: "ρ", sigma: "σ", tau: "τ",
  phi: "φ", chi: "χ", psi: "ψ", omega: "ω", Gamma: "Γ", Delta: "Δ", Lambda: "Λ",
  Sigma: "Σ", Upsilon: "Υ", Phi: "Φ", Psi: "Ψ", Omega: "Ω",
  to: "→", rightarrow: "→", leftarrow: "←", times: "×", pm: "±", sim: "~",
  approx: "≈", geq: "≥", leq: "≤", ell: "ℓ", infty: "∞", prime: "′",
};
const SUP: Record<string, string> = {
  "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴", "5": "⁵", "6": "⁶", "7": "⁷",
  "8": "⁸", "9": "⁹", "+": "⁺", "-": "⁻", "*": "*",
};
const SUB: Record<string, string> = {
  "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄", "5": "₅", "6": "₆", "7": "₇",
  "8": "₈", "9": "₉", "+": "₊", "-": "₋",
};

function script(text: string, map: Record<string, string>, marker: string): string {
  const chars = [...text];
  return chars.every((c) => c in map) ? chars.map((c) => map[c]).join("") : `${marker}${text}`;
}

export function latexToText(input: string): string {
  let s = input;
  // \overline{t} -> t̄ (combining overline), \bar{b} -> b̄
  s = s.replace(/\\(?:overline|bar)\{([^{}]*)\}/g, (_, x: string) => `${x}̄`);
  s = s.replace(/\\sqrt\{([^{}]*)\}/g, "√$1");
  s = s.replace(/\\(?:text|mathrm|textrm|mathit|mbox|rm|bf|it)\{([^{}]*)\}/g, "$1");
  s = s.replace(/\\([A-Za-z]+)/g, (m, name: string) => SYMBOLS[name] ?? m.slice(1));
  s = s.replace(/\^\{([^{}]*)\}|\^(\S)/g, (_, a?: string, b?: string) => script(a ?? b ?? "", SUP, "^"));
  s = s.replace(/_\{([^{}]*)\}|_(\S)/g, (_, a?: string, b?: string) => script(a ?? b ?? "", SUB, "_"));
  s = s.replace(/[${}]/g, "").replace(/\\,|~/g, " ");
  return s.replace(/\s+/g, " ").trim();
}
