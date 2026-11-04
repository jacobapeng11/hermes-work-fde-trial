type SnakeToCamel<S extends string> = S extends `${infer Head}_${infer Tail}`
  ? `${Head}${Capitalize<SnakeToCamel<Tail>>}`
  : S;

export type Camelize<T> = T extends Array<infer Item>
  ? Camelize<Item>[]
  : T extends object
    ? { [K in keyof T as K extends string ? SnakeToCamel<K> : K]: Camelize<T[K]> }
    : T;

export function camelize<T>(value: unknown): T {
  if (Array.isArray(value)) {
    return value.map((item) => camelize(item)) as T;
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, child]) => [
        key.replace(/_([a-z])/g, (_, char: string) => char.toUpperCase()),
        camelize(child)
      ])
    ) as T;
  }
  return value as T;
}

export function decamelize(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map((item) => decamelize(item));
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, child]) => [
        key.replace(/[A-Z]/g, (char) => `_${char.toLowerCase()}`),
        decamelize(child)
      ])
    );
  }
  return value;
}
