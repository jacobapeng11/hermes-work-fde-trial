import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type UIEventHandler
} from "react";

const ANCHOR_TOP_INSET_PX = 12;
const LATEST_RESUME_THRESHOLD_PX = 80;
const SCROLL_EPSILON_PX = 1;

type ScrollMode = "turn-anchor" | "follow-latest" | "manual-history";

type TurnAnchor = {
  sessionId: string;
  messageId: string;
};

type HistoryAnchor = {
  messageId: string;
  offsetTop: number;
  scrollTop: number;
};

type TurnViewport = {
  fits: boolean;
  anchorScrollTop: number;
  latestScrollTop: number;
};

type UseAnchoredChatScrollOptions = {
  sessionId: string | null;
  view: string;
  messages: readonly unknown[];
  streaming: boolean;
};

function directMessageNodes(feed: HTMLDivElement): HTMLElement[] {
  const inner = feed.querySelector<HTMLElement>(".messageFeedInner");
  if (!inner) return [];
  return Array.from(inner.children).filter((node): node is HTMLElement => (
    node instanceof HTMLElement && Boolean(node.dataset.messageId)
  ));
}

function turnNodes(feed: HTMLDivElement, anchorMessageId: string): HTMLElement[] {
  const nodes = directMessageNodes(feed);
  const anchorIndex = nodes.findIndex((node) => (
    node.dataset.messageId === anchorMessageId
  ));
  return anchorIndex < 0 ? [] : nodes.slice(anchorIndex);
}

function latestOverflow(feed: HTMLDivElement, anchorMessageId: string): number | null {
  const nodes = turnNodes(feed, anchorMessageId);
  const latest = nodes[nodes.length - 1];
  if (!latest) return null;
  return latest.getBoundingClientRect().bottom - feed.getBoundingClientRect().bottom;
}

function turnViewport(feed: HTMLDivElement, anchorMessageId: string): TurnViewport | null {
  const nodes = turnNodes(feed, anchorMessageId);
  const anchor = nodes[0];
  const latest = nodes[nodes.length - 1];
  if (!anchor || !latest) return null;

  const feedTop = feed.getBoundingClientRect().top;
  const anchorTop = anchor.getBoundingClientRect().top - feedTop + feed.scrollTop;
  const latestBottom = latest.getBoundingClientRect().bottom - feedTop + feed.scrollTop;
  const anchorScrollTop = anchorTop - ANCHOR_TOP_INSET_PX;
  const latestScrollTop = latestBottom - feed.clientHeight;
  return {
    fits: latestBottom - anchorScrollTop <= feed.clientHeight + SCROLL_EPSILON_PX,
    anchorScrollTop,
    latestScrollTop
  };
}

export function useAnchoredChatScroll({
  sessionId,
  view,
  messages,
  streaming
}: UseAnchoredChatScrollOptions) {
  const messageFeedRef = useRef<HTMLDivElement | null>(null);
  const previousSessionRef = useRef<string | null>(null);
  const modeRef = useRef<ScrollMode>("follow-latest");
  const forceLatestRef = useRef(false);
  const expectedScrollTopRef = useRef<number | null>(null);
  const lastScrollTopRef = useRef(0);
  const userScrollIntentUntilRef = useRef(0);
  const pointerScrollStartRef = useRef<{
    pointerId: number;
    clientY: number;
  } | null>(null);
  const mouseScrollStartRef = useRef<number | null>(null);
  const pendingHistoryAnchorRef = useRef<HistoryAnchor | null>(null);
  const [turnAnchor, setTurnAnchor] = useState<TurnAnchor | null>(null);
  const [turnSpacerHeight, setTurnSpacerHeight] = useState(0);
  const [scrollRequest, setScrollRequest] = useState(0);
  const [showReturnToLatest, setShowReturnToLatest] = useState(false);

  const activeAnchor = turnAnchor?.sessionId === sessionId
    ? turnAnchor
    : null;

  const setProgrammaticScrollTop = useCallback((feed: HTMLDivElement, next: number) => {
    feed.scrollTop = Math.max(0, next);
    expectedScrollTopRef.current = feed.scrollTop;
    lastScrollTopRef.current = feed.scrollTop;
  }, []);

  const clearTurnAnchor = useCallback(() => {
    setTurnAnchor(null);
    setTurnSpacerHeight(0);
  }, []);

  const reconcileTurnViewport = useCallback((feed: HTMLDivElement, anchorMessageId: string) => {
    const viewport = turnViewport(feed, anchorMessageId);
    if (!viewport) return null;
    if (viewport.fits) {
      setTurnSpacerHeight(Math.max(0, feed.clientHeight));
      setProgrammaticScrollTop(feed, viewport.anchorScrollTop);
      modeRef.current = "turn-anchor";
      return "fits" as const;
    }
    setTurnSpacerHeight(0);
    setProgrammaticScrollTop(feed, viewport.latestScrollTop);
    modeRef.current = "follow-latest";
    return "overflow" as const;
  }, [setProgrammaticScrollTop]);

  const requestScrollToLatest = useCallback(() => {
    pendingHistoryAnchorRef.current = null;
    forceLatestRef.current = true;
    setScrollRequest((request) => request + 1);
  }, []);

  const requestFollowLatestUnlessManual = useCallback(() => {
    if (modeRef.current === "manual-history") return;
    setScrollRequest((request) => request + 1);
  }, []);

  const prepareHistoryRefresh = useCallback(() => {
    const feed = messageFeedRef.current;
    if (!feed || modeRef.current !== "manual-history") return;
    const feedTop = feed.getBoundingClientRect().top;
    const firstVisible = directMessageNodes(feed).find(
      (node) => node.getBoundingClientRect().bottom > feedTop
    );
    if (!firstVisible?.dataset.messageId) return;
    pendingHistoryAnchorRef.current = {
      messageId: firstVisible.dataset.messageId,
      offsetTop: firstVisible.getBoundingClientRect().top - feedTop,
      scrollTop: feed.scrollTop
    };
  }, []);

  const anchorTurn = useCallback((messageId: string) => {
    if (!sessionId) return;
    const feed = messageFeedRef.current;
    modeRef.current = "turn-anchor";
    forceLatestRef.current = false;
    setShowReturnToLatest(false);
    setTurnSpacerHeight(Math.max(0, feed?.clientHeight ?? 0));
    setTurnAnchor({ sessionId, messageId });
  }, [sessionId]);

  const resumeLatest = useCallback(() => {
    const feed = messageFeedRef.current;
    if (!feed || !activeAnchor) {
      requestScrollToLatest();
      return;
    }
    const overflow = latestOverflow(feed, activeAnchor.messageId);
    if (overflow !== null && overflow > SCROLL_EPSILON_PX) {
      setProgrammaticScrollTop(feed, feed.scrollTop + overflow);
    }
    modeRef.current = "follow-latest";
    setShowReturnToLatest(false);
  }, [activeAnchor, requestScrollToLatest, setProgrammaticScrollTop]);

  const onMessageFeedScroll = useCallback<UIEventHandler<HTMLDivElement>>((event) => {
    const feed = event.currentTarget;
    const currentScrollTop = feed.scrollTop;
    const expected = expectedScrollTopRef.current;
    const userInitiated = userScrollIntentUntilRef.current >= Date.now();
    const movedUp = currentScrollTop < lastScrollTopRef.current;
    const movedDown = currentScrollTop > lastScrollTopRef.current;
    const matchesExpected = expected !== null
      && Math.abs(currentScrollTop - expected) <= SCROLL_EPSILON_PX;
    if (matchesExpected && !(userInitiated && movedUp)) {
      expectedScrollTopRef.current = null;
      lastScrollTopRef.current = currentScrollTop;
      userScrollIntentUntilRef.current = 0;
      return;
    }
    expectedScrollTopRef.current = null;

    const meaningfulMoveUp = userInitiated
      ? movedUp
      : currentScrollTop < lastScrollTopRef.current - SCROLL_EPSILON_PX;
    lastScrollTopRef.current = currentScrollTop;
    userScrollIntentUntilRef.current = 0;
    if (meaningfulMoveUp && userInitiated) {
      modeRef.current = "manual-history";
      if (!streaming) clearTurnAnchor();
      setShowReturnToLatest(streaming);
      return;
    }
    if (modeRef.current !== "manual-history") return;

    const distanceToLatest = activeAnchor
      ? latestOverflow(feed, activeAnchor.messageId)
      : feed.scrollHeight - feed.clientHeight - currentScrollTop;
    if (
      movedDown
      && distanceToLatest !== null
      && distanceToLatest <= LATEST_RESUME_THRESHOLD_PX
    ) {
      modeRef.current = "follow-latest";
      setShowReturnToLatest(false);
    }
  }, [activeAnchor, clearTurnAnchor, streaming]);

  useEffect(() => {
    const feed = messageFeedRef.current;
    if (!feed) return;
    const markUserScrollIntent = () => {
      userScrollIntentUntilRef.current = Date.now() + 1_000;
    };
    const markKeyboardScrollIntent = (event: KeyboardEvent) => {
      if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "].includes(event.key)) {
        markUserScrollIntent();
      }
    };
    feed.addEventListener("wheel", markUserScrollIntent, { passive: true });
    feed.addEventListener("touchmove", markUserScrollIntent, { passive: true });
    feed.addEventListener("keydown", markKeyboardScrollIntent);
    const startPointerScroll = (event: PointerEvent) => {
      const target = event.target;
      if (target !== feed && !feed.contains(target as Node)) return;
      pointerScrollStartRef.current = {
        pointerId: event.pointerId,
        clientY: event.clientY
      };
    };
    const detectPointerScroll = (event: PointerEvent) => {
      const start = pointerScrollStartRef.current;
      if (
        start
        && start.pointerId === event.pointerId
        && Math.abs(event.clientY - start.clientY) > 0
      ) {
        markUserScrollIntent();
      }
    };
    const finishPointerScroll = () => {
      pointerScrollStartRef.current = null;
    };
    const startMouseScroll = (event: MouseEvent) => {
      const target = event.target;
      const insideFeed = target === feed || feed.contains(target as Node);
      const feedRect = feed.getBoundingClientRect();
      const inScrollbarGutter = event.clientX >= feedRect.right - 20
        && event.clientX <= feedRect.right
        && event.clientY >= feedRect.top
        && event.clientY <= feedRect.bottom;
      if (!insideFeed && !inScrollbarGutter) return;
      mouseScrollStartRef.current = event.clientY;
    };
    const markDocumentMouseIntent = (event: MouseEvent) => {
      if (event.button === 0) markUserScrollIntent();
    };
    const detectMouseScroll = (event: MouseEvent) => {
      const start = mouseScrollStartRef.current;
      if (start !== null && Math.abs(event.clientY - start) > 0) {
        markUserScrollIntent();
      }
    };
    const finishMouseScroll = () => {
      mouseScrollStartRef.current = null;
    };
    feed.addEventListener("pointerdown", startPointerScroll);
    feed.addEventListener("pointermove", detectPointerScroll);
    feed.addEventListener("pointerup", finishPointerScroll);
    feed.addEventListener("pointercancel", finishPointerScroll);
    document.addEventListener("mousedown", startMouseScroll, { capture: true });
    document.addEventListener("mousedown", markDocumentMouseIntent, { capture: true });
    window.addEventListener("mousemove", detectMouseScroll, { passive: true });
    window.addEventListener("mouseup", finishMouseScroll);
    return () => {
      feed.removeEventListener("wheel", markUserScrollIntent);
      feed.removeEventListener("touchmove", markUserScrollIntent);
      feed.removeEventListener("keydown", markKeyboardScrollIntent);
      feed.removeEventListener("pointerdown", startPointerScroll);
      feed.removeEventListener("pointermove", detectPointerScroll);
      feed.removeEventListener("pointerup", finishPointerScroll);
      feed.removeEventListener("pointercancel", finishPointerScroll);
      document.removeEventListener("mousedown", startMouseScroll, { capture: true });
      document.removeEventListener("mousedown", markDocumentMouseIntent, { capture: true });
      window.removeEventListener("mousemove", detectMouseScroll);
      window.removeEventListener("mouseup", finishMouseScroll);
    };
  }, [view]);

  useLayoutEffect(() => {
    const feed = messageFeedRef.current;
    if (!feed || view !== "chat") return;
    const sessionChanged = previousSessionRef.current !== sessionId;
    previousSessionRef.current = sessionId;

    if (sessionChanged || forceLatestRef.current) {
      pendingHistoryAnchorRef.current = null;
      forceLatestRef.current = false;
      modeRef.current = "follow-latest";
      setShowReturnToLatest(false);
      if (turnAnchor) clearTurnAnchor();
      setProgrammaticScrollTop(feed, feed.scrollHeight);
      return;
    }
    if (modeRef.current === "manual-history") {
      const pending = pendingHistoryAnchorRef.current;
      pendingHistoryAnchorRef.current = null;
      if (!pending) return;
      const historyAnchor = directMessageNodes(feed).find(
        (node) => node.dataset.messageId === pending.messageId
      );
      if (historyAnchor) {
        const nextOffset = historyAnchor.getBoundingClientRect().top
          - feed.getBoundingClientRect().top;
        setProgrammaticScrollTop(
          feed,
          feed.scrollTop + nextOffset - pending.offsetTop
        );
      } else {
        setProgrammaticScrollTop(
          feed,
          Math.min(
            pending.scrollTop,
            Math.max(0, feed.scrollHeight - feed.clientHeight)
          )
        );
      }
      return;
    }
    if (!activeAnchor) {
      if (modeRef.current === "follow-latest") {
        setProgrammaticScrollTop(feed, feed.scrollHeight);
      }
      return;
    }

    if (!reconcileTurnViewport(feed, activeAnchor.messageId)) {
      modeRef.current = "follow-latest";
      clearTurnAnchor();
    }
  }, [
    activeAnchor,
    clearTurnAnchor,
    messages,
    reconcileTurnViewport,
    scrollRequest,
    sessionId,
    setProgrammaticScrollTop,
    turnAnchor,
    view
  ]);

  useLayoutEffect(() => {
    if (streaming || !activeAnchor) return;
    if (modeRef.current !== "manual-history") {
      const feed = messageFeedRef.current;
      if (feed) reconcileTurnViewport(feed, activeAnchor.messageId);
    } else {
      clearTurnAnchor();
    }
    setShowReturnToLatest(false);
  }, [activeAnchor, clearTurnAnchor, reconcileTurnViewport, streaming]);

  useEffect(() => {
    if (modeRef.current === "manual-history" && !streaming) {
      setShowReturnToLatest(false);
    }
  }, [streaming]);

  useEffect(() => {
    const feed = messageFeedRef.current;
    if (!feed || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (activeAnchor) {
        setTurnSpacerHeight(Math.max(0, feed.clientHeight));
      }
      if (modeRef.current === "manual-history") return;
      if (activeAnchor) {
        reconcileTurnViewport(feed, activeAnchor.messageId);
        return;
      }
      if (!activeAnchor && modeRef.current === "follow-latest") {
        setProgrammaticScrollTop(feed, feed.scrollHeight);
      }
    });
    observer.observe(feed);
    const inner = feed.querySelector<HTMLElement>(".messageFeedInner");
    if (inner) observer.observe(inner);
    return () => observer.disconnect();
  }, [activeAnchor, reconcileTurnViewport, setProgrammaticScrollTop]);

  return {
    messageFeedRef,
    onMessageFeedScroll,
    requestScrollToLatest,
    requestFollowLatestUnlessManual,
    prepareHistoryRefresh,
    anchorTurn,
    resumeLatest,
    turnSpacerHeight: activeAnchor ? turnSpacerHeight : 0,
    showReturnToLatest
  };
}
