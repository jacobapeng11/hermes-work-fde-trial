import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useExpertEditorAnchors } from "./useExpertEditorAnchors";

class FakeIntersectionObserver {
  observe = vi.fn();
  disconnect = vi.fn();
  unobserve = vi.fn();
  takeRecords = vi.fn(() => []);
  root = null;
  rootMargin = "0px";
  thresholds = [0];
}

function Harness() {
  const anchors = useExpertEditorAnchors("create", true);
  return <div className="expertDialogBody" data-testid="editor-body">
    {['basic', 'visibility', 'skill', 'mcp', 'knowledge'].map((id) => (
      <section id={`create-${id}`} key={id}>{id}</section>
    ))}
    <output aria-label="active section">{anchors.active}</output>
  </div>;
}

describe("useExpertEditorAnchors", () => {
  beforeEach(() => {
    vi.stubGlobal("IntersectionObserver", FakeIntersectionObserver);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("activates the final section when the editor body reaches the bottom", () => {
    render(<Harness/>);
    const body = screen.getByTestId("editor-body");
    Object.defineProperties(body, {
      scrollHeight: { configurable: true, value: 1000 },
      clientHeight: { configurable: true, value: 200 },
      scrollTop: { configurable: true, value: 800 },
    });

    fireEvent.scroll(body);

    expect(screen.getByLabelText("active section")).toHaveTextContent("knowledge");
  });
});
