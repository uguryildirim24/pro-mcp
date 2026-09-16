// Loaded into the ChatGPT page by `pro-mcp start`. Keeps keyboard focus in the message box,
// so text that herdr types into the pane (`herdr agent prompt`) always lands there.
(() => {
  const editable = (el) =>
    !!el && (el.isContentEditable || el.tagName === "TEXTAREA" || el.tagName === "INPUT");
  const menuOpen = () => document.querySelector('[role="dialog"], [role="menu"], [role="listbox"]');
  setInterval(() => {
    const box = document.querySelector("#prompt-textarea");
    if (!box || editable(document.activeElement) || menuOpen()) return;
    box.focus();
  }, 500);
})();
