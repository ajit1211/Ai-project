import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { Markdown, safeUrl } from "./Markdown";

describe("Markdown", () => {
  afterEach(cleanup);

  it("renders a GitHub table", () => {
    render(<Markdown text={"| API | Style |\n| --- | --- |\n| REST | resources |\n| GraphQL | queries |"} />);
    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("row")).toHaveLength(3);
    expect(within(table).getByRole("columnheader", { name: "API" })).toBeTruthy();
    expect(within(table).getByRole("cell", { name: "queries" })).toBeTruthy();
  });

  it("renders lists, headings and inline code", () => {
    render(<Markdown text={"## Steps\n\n1. Install `uv`\n2. Run it\n\n- a\n- b"} />);
    expect(screen.getByRole("heading", { level: 2, name: "Steps" })).toBeTruthy();
    expect(screen.getAllByRole("list")).toHaveLength(2);
    expect(screen.getByText("uv").tagName).toBe("CODE");
  });

  it("highlights a fenced code block by its language", () => {
    const { container } = render(<Markdown text={"```python\ndef hello():\n    return 42\n```"} />);
    const code = container.querySelector("pre > code");
    expect(code?.classList.contains("hljs")).toBe(true);
    expect(code?.classList.contains("language-python")).toBe(true);
    expect(code?.querySelector(".hljs-keyword")?.textContent).toBe("def");
    expect(code?.textContent).toBe("def hello():\n    return 42\n");
  });

  it("opens safe links in a new tab without access to this one", () => {
    render(<Markdown text="See [the docs](https://example.com/docs) or [mail us](mailto:team@example.com)." />);
    const docs = screen.getByRole("link", { name: "the docs" });
    expect(docs.getAttribute("href")).toBe("https://example.com/docs");
    expect(docs.getAttribute("target")).toBe("_blank");
    expect(docs.getAttribute("rel")).toBe("noopener noreferrer");
    expect(screen.getByRole("link", { name: "mail us" }).getAttribute("href")).toBe("mailto:team@example.com");
  });

  it("neutralizes javascript:, data: and vbscript: links", () => {
    const { container } = render(
      <Markdown
        text={
          "[click me](javascript:alert(1)) [data](data:text/html;base64,PHNjcmlwdD4=) [vb](vbscript:msgbox(1))"
        }
      />,
    );
    expect(screen.queryByRole("link")).toBeNull();
    expect(container.querySelector("[href]")).toBeNull();
    expect(screen.getByText("click me").tagName).toBe("SPAN");
  });

  it("shows raw HTML as text instead of rendering it", () => {
    const { container } = render(
      <Markdown text={'Before\n\n<script>alert("x")</script>\n\n<img src="x" onerror="alert(1)">'} />,
    );
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain('<script>alert("x")</script>');
  });

  it("shows images as links instead of loading them", () => {
    const { container } = render(<Markdown text="![a chart](https://example.com/chart.png)" />);
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByRole("link", { name: "[a chart]" }).getAttribute("href")).toBe(
      "https://example.com/chart.png",
    );
  });
});

describe("safeUrl", () => {
  it("keeps absolute http, https and mailto URLs", () => {
    expect(safeUrl("https://example.com/a?b=1#c")).toBe("https://example.com/a?b=1#c");
    expect(safeUrl("HTTP://EXAMPLE.COM")).toBe("http://example.com/");
    expect(safeUrl("mailto:team@example.com")).toBe("mailto:team@example.com");
  });

  it("drops every other scheme and anything relative", () => {
    for (const url of [
      "javascript:alert(1)",
      "JaVaScRiPt:alert(1)",
      " javascript:alert(1)",
      "java\tscript:alert(1)",
      "vbscript:msgbox(1)",
      "data:text/html,<script>alert(1)</script>",
      "file:///etc/passwd",
      "/settings",
      "#section",
      "//evil.example.com",
    ]) {
      expect(safeUrl(url), url).toBeNull();
    }
  });
});
