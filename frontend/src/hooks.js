import { useEffect, useRef } from "react";

/** Run `fn` now and then every `ms`; restarts immediately when `deps` change. */
export function usePolling(fn, ms, deps) {
  const latest = useRef(fn);
  latest.current = fn;
  useEffect(() => {
    const run = () => latest.current();
    run();
    const timer = setInterval(run, ms);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
}
