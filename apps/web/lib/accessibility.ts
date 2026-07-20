"use client";

import { type KeyboardEvent as ReactKeyboardEvent, type RefObject, useEffect, useRef } from "react";

const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex=\"-1\"])",
].join(",");

function visibleFocusable(container: HTMLElement): HTMLElement[] {
  return [...container.querySelectorAll<HTMLElement>(FOCUSABLE)]
    .filter(element => element.offsetParent !== null && element.getAttribute("aria-hidden") !== "true");
}

/**
 * Gives modal surfaces predictable keyboard behavior: initial focus, Escape,
 * a contained Tab order, and focus restoration to the invoking control.
 */
export function useDialogFocus<T extends HTMLElement>({
  active,
  containerRef,
  initialFocusRef,
  onClose,
  closeOnEscape = true,
}: {
  active: boolean;
  containerRef: RefObject<T | null>;
  initialFocusRef?: RefObject<HTMLElement | null>;
  onClose: () => void;
  closeOnEscape?: boolean;
}) {
  const closeRef = useRef(onClose);

  useEffect(() => {
    closeRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!active) return;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const frame = window.requestAnimationFrame(() => {
      const container = containerRef.current;
      if (!container) return;
      (initialFocusRef?.current || visibleFocusable(container)[0] || container).focus();
    });

    function onKeyDown(event: KeyboardEvent) {
      const container = containerRef.current;
      if (!container) return;
      if (event.key === "Escape" && closeOnEscape) {
        event.preventDefault();
        closeRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = visibleFocusable(container);
      if (focusable.length === 0) {
        event.preventDefault();
        container.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && (document.activeElement === first || !container.contains(document.activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", onKeyDown);
    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener("keydown", onKeyDown);
      window.requestAnimationFrame(() => previousFocus?.focus());
    };
  }, [active, closeOnEscape, containerRef, initialFocusRef]);
}

/** Implements the expected Arrow/Home/End behavior for an open ARIA menu. */
export function moveMenuFocus(container: HTMLElement, event: KeyboardEvent | ReactKeyboardEvent) {
  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
  const items = [...container.querySelectorAll<HTMLElement>('[role="menuitem"], [role="menuitemradio"]')]
    .filter(element => !element.hasAttribute("disabled") && element.offsetParent !== null);
  if (items.length === 0) return;
  event.preventDefault();
  const current = items.indexOf(document.activeElement as HTMLElement);
  if (event.key === "Home") items[0].focus();
  else if (event.key === "End") items[items.length - 1].focus();
  else if (event.key === "ArrowDown") items[(current + 1 + items.length) % items.length].focus();
  else items[(current - 1 + items.length) % items.length].focus();
}
