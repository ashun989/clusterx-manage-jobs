import { readAfsCatalog } from "./afs-catalog";
import { fillDevelopmentForm } from "./fill";
import { isAfsCatalogRequest, isFillRequest, scrubProfileSecrets } from "./types";

if (typeof chrome !== "undefined" && chrome.runtime?.onMessage) {
  chrome.runtime.onMessage.addListener((message: unknown, _sender, sendResponse) => {
    if (isAfsCatalogRequest(message)) {
      const isListPage = /^\/[^/]+\/afs\/list\/?$/.test(window.location.pathname);
      if (!isListPage) {
        sendResponse({ ok: false, catalog: {} });
        return false;
      }
      void readAfsCatalog(document, message.ids)
        .then((catalog) => sendResponse({ ok: true, catalog }))
        .catch(() => sendResponse({ ok: false, catalog: {} }));
      return true;
    }
    if (isFillRequest(message)) {
      const profile = message.profile;
      void fillDevelopmentForm(profile, document, window.location.href, message.afsCatalog)
        .then(sendResponse)
        .catch(() => sendResponse({
          ok: false,
          fatal: true,
          items: [{
            key: "unexpected",
            label: "页面填充",
            status: "error",
            message: "页面结构与预期不一致",
          }],
        }))
        .finally(() => scrubProfileSecrets(profile));
      return true;
    }
    return false;
  });
}
