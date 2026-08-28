import { useEffect, useRef, type RefObject } from "react";

const focusableSelector = "button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])";

export function useDialogFocus(container: RefObject<HTMLElement | null>, onEscape: () => void, enabled = true) {
  const escape = useRef(onEscape);
  escape.current = onEscape;
  useEffect(() => {
    if (!enabled) return;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const frame = window.requestAnimationFrame(() => container.current?.querySelector<HTMLElement>(focusableSelector)?.focus());
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); escape.current(); return; }
      if (event.key !== "Tab" || !container.current) return;
      const focusable = [...container.current.querySelectorAll<HTMLElement>(focusableSelector)].filter((item) => !item.hidden && item.offsetParent !== null);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable.at(-1)!;
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    window.addEventListener("keydown", keydown);
    return () => {
      window.cancelAnimationFrame(frame);
      window.removeEventListener("keydown", keydown);
      document.body.style.overflow = previousOverflow;
      previousFocus?.focus();
    };
  }, [container, enabled]);
}
