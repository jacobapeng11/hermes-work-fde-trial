// Node's type-stripping loader resolves ESM specifiers strictly, so the
// extensionless relative imports used across src/main/*.ts (fine for
// electron-vite/rollup) fail under `node --test`. This test-only resolver
// retries a failed specifier with an explicit `.ts` extension.
export async function resolve(specifier, context, next) {
  try {
    return await next(specifier, context);
  } catch (error) {
    if (error?.code === "ERR_MODULE_NOT_FOUND" && !specifier.endsWith(".ts") && !specifier.endsWith(".mjs") && !specifier.endsWith(".json")) {
      return next(`${specifier}.ts`, context);
    }
    throw error;
  }
}
