import { useEffect, useState } from "react";

// Minimal hash router: "#/chat/conv_123" -> { page: "chat", rest: ["conv_123"] }.
// Hash routing works from any static file server and keeps deep links inside the PWA scope.
export interface Route {
  page: string;
  rest: string[];
  query: URLSearchParams;
}

export function parseHash(hash: string): Route {
  const raw = hash.replace(/^#\/?/, "");
  const [path, qs = ""] = raw.split("?");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  return { page: parts[0] ?? "chat", rest: parts.slice(1), query: new URLSearchParams(qs) };
}

export function navigate(path: string): void {
  const target = path.startsWith("#") ? path : `#${path.startsWith("/") ? path : `/${path}`}`;
  if (window.location.hash !== target) window.location.hash = target;
}

export function useRoute(): Route {
  const [route, setRoute] = useState(() => parseHash(window.location.hash));
  useEffect(() => {
    const onChange = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}
