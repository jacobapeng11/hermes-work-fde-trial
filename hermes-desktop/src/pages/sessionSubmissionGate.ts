export type ReleaseSubmission = () => void;

interface SessionTail {
  token: symbol;
  released: Promise<void>;
}

/**
 * A short, page-local ingress gate. It only orders when Runtime network
 * submission may start; Runtime remains the durable/global ordering source.
 */
export class SessionSubmissionGate {
  private readonly tails = new Map<string, SessionTail>();

  async run<T>(
    sessionId: string,
    dispatch: (release: ReleaseSubmission) => Promise<T>
  ): Promise<T> {
    const previous = this.tails.get(sessionId)?.released
      ?? Promise.resolve();
    let releaseCurrent: () => void = () => undefined;
    const released = new Promise<void>((resolve) => {
      releaseCurrent = resolve;
    });
    const token = Symbol(sessionId);
    this.tails.set(sessionId, { token, released });
    await previous;

    let releasedOnce = false;
    const release = () => {
      if (releasedOnce) return;
      releasedOnce = true;
      releaseCurrent();
      if (this.tails.get(sessionId)?.token === token) {
        this.tails.delete(sessionId);
      }
    };
    try {
      return await dispatch(release);
    } finally {
      // Local validation/network failure is also a settled gate outcome.
      release();
    }
  }
}
