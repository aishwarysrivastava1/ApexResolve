// One small hook for every screen that shows server data: load on mount, expose reload().
import { useCallback, useEffect, useState } from "react";
import { errorMessage, getJson } from "./api";

export interface Loaded<T> {
  data: T | null;
  error: string | null;
  reload: () => void;
}

export function useData<T>(path: string | null): Loaded<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [version, setVersion] = useState(0);

  useEffect(() => {
    // a null path means "nothing to load yet"
    if (path === null) {
      return;
    }
    let cancelled = false;
    getJson<T>(path)
      .then((result) => {
        if (!cancelled) {
          setData(result);
          setError(null);
        }
      })
      .catch((failure: unknown) => {
        if (!cancelled) {
          setError(errorMessage(failure));
        }
      });
    // ignore a response that arrives after the screen changed
    return () => {
      cancelled = true;
    };
  }, [path, version]);

  const reload = useCallback(() => setVersion((v) => v + 1), []);
  return { data, error, reload };
}
