/* A figure that counts to its new value instead of jumping.
 *
 * The console polls every two seconds. Repainting the tiles silently made a
 * running system look like a still image, and worse, it hid change: a
 * number that goes from 280 to 341 between two frames is indistinguishable
 * from one that did not move unless you were watching that pixel.
 *
 * Counting is not decoration here, it is the signal that something
 * happened. It is skipped entirely under `prefers-reduced-motion`, and on
 * the first value, because animating from zero on page load is the
 * decorative version of this and says nothing.
 */

import { useEffect, useRef, useState } from "react";

const DURATION = 520;

function reducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

export function useCountUp(target: number): number {
  const [shown, setShown] = useState(target);
  const from = useRef(target);
  const frame = useRef(0);
  const seen = useRef(false);

  useEffect(() => {
    if (!seen.current) {
      // First render: show the real value rather than counting up to it
      // from nothing.
      seen.current = true;
      from.current = target;
      setShown(target);
      return;
    }
    if (reducedMotion() || !Number.isFinite(target)) {
      from.current = target;
      setShown(target);
      return;
    }

    const start = performance.now();
    const origin = from.current;
    const delta = target - origin;
    if (delta === 0) return;

    const step = (now: number) => {
      const t = Math.min(1, (now - start) / DURATION);
      // Ease out: the value lands softly rather than stopping dead, which
      // is what makes it read as settling on a number.
      const eased = 1 - (1 - t) ** 3;
      setShown(origin + delta * eased);
      if (t < 1) {
        frame.current = requestAnimationFrame(step);
      } else {
        from.current = target;
      }
    };
    frame.current = requestAnimationFrame(step);
    return () => cancelAnimationFrame(frame.current);
  }, [target]);

  return shown;
}

/** Ids seen on the previous render, so a component can tell which rows are
 *  new and animate only those. Returns a set, and updates after the render
 *  that used it. */
export function useArrivals(ids: string[]): Set<string> {
  const previous = useRef<Set<string> | null>(null);
  const fresh = new Set<string>();
  if (previous.current) {
    for (const id of ids) if (!previous.current.has(id)) fresh.add(id);
  }
  useEffect(() => {
    previous.current = new Set(ids);
  });
  return fresh;
}
