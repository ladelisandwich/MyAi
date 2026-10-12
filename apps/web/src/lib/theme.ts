export type Theme = "system" | "light" | "dark";
const KEY = "homemesh.theme";

export function getTheme(): Theme {
  return (localStorage.getItem(KEY) as Theme) || "system";
}

export function applyTheme(theme: Theme = getTheme()): void {
  const dark = theme === "dark" || (theme === "system" && window.matchMedia?.("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
}

export function setTheme(theme: Theme): void {
  localStorage.setItem(KEY, theme);
  applyTheme(theme);
}

export function watchSystemTheme(): void {
  window.matchMedia?.("(prefers-color-scheme: dark)").addEventListener?.("change", () => applyTheme());
}
