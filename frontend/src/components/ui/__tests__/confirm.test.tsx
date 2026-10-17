import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { Providers } from "@/test/render";
import { useConfirm } from "../confirm";

function Harness() {
  const confirm = useConfirm();
  const [result, setResult] = useState("none");
  return (
    <>
      <button
        onClick={async () =>
          setResult(
            String(
              await confirm({
                title: "Delete this document?",
                description: "It cannot be undone.",
                confirmLabel: "Delete document",
                destructive: true,
              }),
            ),
          )
        }
      >
        Delete
      </button>
      <output data-testid="result">{result}</output>
    </>
  );
}

const setup = () => {
  render(
    <Providers>
      <Harness />
    </Providers>,
  );
  return userEvent.setup();
};

describe("confirmation dialog", () => {
  it("asks first, with Cancel focused so Enter never destroys data by accident", async () => {
    const user = setup();
    await user.click(screen.getByRole("button", { name: "Delete" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Delete this document?" });
    expect(dialog).toHaveTextContent("It cannot be undone.");
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
    expect(screen.getByTestId("result")).toHaveTextContent("none");
  });

  it("resolves true on confirm", async () => {
    const user = setup();
    await user.click(screen.getByRole("button", { name: "Delete" }));
    await user.click(await screen.findByRole("button", { name: "Delete document" }));
    await waitFor(() => expect(screen.getByTestId("result")).toHaveTextContent("true"));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("resolves false on cancel", async () => {
    const user = setup();
    await user.click(screen.getByRole("button", { name: "Delete" }));
    await user.click(await screen.findByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.getByTestId("result")).toHaveTextContent("false"));
  });

  it("closes on Escape and returns focus to the button that opened it", async () => {
    const user = setup();
    const opener = screen.getByRole("button", { name: "Delete" });
    await user.click(opener);
    await screen.findByRole("alertdialog");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.getByTestId("result")).toHaveTextContent("false"));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    await waitFor(() => expect(opener).toHaveFocus());
  });
});
