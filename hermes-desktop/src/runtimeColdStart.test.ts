import { describe, expect, it } from "vitest";

import { isCriticalRuntimeError } from "./runtimeColdStart";

function errorWithStatus(
  message: string,
  status: number
): Error & { status: number } {
  const error = new Error(message) as Error & { status: number };
  error.status = status;
  return error;
}

describe("runtime cold-start error handling", () => {
  describe("isCriticalRuntimeError", () => {
    it("treats runtime-not-ready text as a waiting state", () => {
      expect(isCriticalRuntimeError(new Error("runtime is not ready"))).toBe(
        false
      );
    });

    it("treats a 503 status as a waiting state", () => {
      expect(
        isCriticalRuntimeError(errorWithStatus("Service unavailable", 503))
      ).toBe(false);
    });

    it("recognizes 503 in a message without matching longer numbers", () => {
      expect(
        isCriticalRuntimeError(new Error("Request failed with status 503"))
      ).toBe(false);
      expect(
        isCriticalRuntimeError(new Error("Request failed with status 5030"))
      ).toBe(true);
    });

    it("marks authorization and not-found failures as critical", () => {
      expect(isCriticalRuntimeError(errorWithStatus("Forbidden", 403))).toBe(
        true
      );
      expect(isCriticalRuntimeError(errorWithStatus("Not found", 404))).toBe(
        true
      );
    });

    it("marks network, generic, and non-error failures as critical", () => {
      expect(isCriticalRuntimeError(new Error("Network error"))).toBe(true);
      expect(isCriticalRuntimeError("error string")).toBe(true);
      expect(isCriticalRuntimeError(null)).toBe(true);
    });
  });
});
