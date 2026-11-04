import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AntdModuleProviders } from "./antd-module";
import { PlatformThemeProvider } from "./platform-theme";

describe("AntdModuleProviders", () => {
  it("在默认暗色主题下正常渲染子内容", () => {
    const { getByText } = render(
      <PlatformThemeProvider>
        <AntdModuleProviders>
          <span>module content</span>
        </AntdModuleProviders>
      </PlatformThemeProvider>
    );
    expect(getByText("module content")).toBeInTheDocument();
  });
});
