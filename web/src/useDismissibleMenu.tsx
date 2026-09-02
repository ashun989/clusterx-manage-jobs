import { useEffect, useRef, useState } from "react";
import type { ComponentPropsWithoutRef, ReactNode, RefObject } from "react";

/**
 * Gives custom popover menus the same outside-click and Escape semantics as a
 * native menu, while keeping the menu's focusable contents intact.
 */
export function useDismissibleMenu<T extends HTMLElement>(open: boolean, onClose: () => void): RefObject<T | null> {
  const ref = useRef<T | null>(null);

  useEffect(() => {
    if (!open) return;
    const handleOutside = (event: Event) => {
      const target = event.target;
      if (target instanceof Node && ref.current && !ref.current.contains(target)) onClose();
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || !ref.current) return;
      event.preventDefault();
      onClose();
    };
    document.addEventListener("pointerdown", handleOutside);
    // Keep click semantics for keyboard-triggered/native test events and
    // browsers that do not expose PointerEvent on a particular input device.
    document.addEventListener("click", handleOutside);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handleOutside);
      document.removeEventListener("click", handleOutside);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open, onClose]);

  return ref;
}

export function DismissibleDetails({ className, summary, summaryProps, children }: {
  className?: string;
  summary: ReactNode;
  summaryProps?: ComponentPropsWithoutRef<"summary">;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const ref = useDismissibleMenu<HTMLDetailsElement>(open, () => setOpen(false));
  return <details
    ref={ref}
    className={className}
    open={open}
  >
    <summary
      {...summaryProps}
      onClick={(event) => {
        summaryProps?.onClick?.(event);
        if (!event.defaultPrevented) {
          event.preventDefault();
          setOpen((value) => !value);
        }
      }}
    >{summary}</summary>
    {children}
  </details>;
}
