// SPDX-License-Identifier: MIT-0
(() => {
  try {
    const value = localStorage.getItem("adlytics.theme");
    document.documentElement.dataset.theme = value === "light" || value === "dark" || value === "system"
      ? value
      : "system";
  } catch {
    document.documentElement.dataset.theme = "system";
  }
})();
