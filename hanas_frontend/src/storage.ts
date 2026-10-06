function readStorage(storage: Storage | undefined, key: string): string | null {
  try {
    return storage?.getItem(key) ?? null
  } catch {
    return null
  }
}

function writeStorage(storage: Storage | undefined, key: string, value: string): void {
  try {
    storage?.setItem(key, value)
  } catch {
    // Storage can be unavailable in private, embedded, or quota-restricted browser contexts.
  }
}

function deleteStorage(storage: Storage | undefined, key: string): void {
  try {
    storage?.removeItem(key)
  } catch {
    // Ignore storage cleanup failures; in-memory UI state remains authoritative.
  }
}

function getLocalStorage(): Storage | undefined {
  try {
    return typeof window === 'undefined' ? undefined : window.localStorage
  } catch {
    return undefined
  }
}

function getSessionStorage(): Storage | undefined {
  try {
    return typeof window === 'undefined' ? undefined : window.sessionStorage
  } catch {
    return undefined
  }
}

export const safeLocalStorage = {
  getItem: (key: string) => readStorage(getLocalStorage(), key),
  setItem: (key: string, value: string) => writeStorage(getLocalStorage(), key, value),
  removeItem: (key: string) => deleteStorage(getLocalStorage(), key),
}

export const safeSessionStorage = {
  getItem: (key: string) => readStorage(getSessionStorage(), key),
  setItem: (key: string, value: string) => writeStorage(getSessionStorage(), key, value),
  removeItem: (key: string) => deleteStorage(getSessionStorage(), key),
}
