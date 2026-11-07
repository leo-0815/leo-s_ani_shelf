/* One navigation tree for desktop and mobile; opening the menu never reads an API. */
window.AniShelfMobileNavigation = (() => {
  let initialized = false, dialog, opener, previousOverflow = "";
  function close() {if (dialog?.open) dialog.close();}
  function init() {
    if (initialized) return;
    initialized = true;
    dialog = document.getElementById("mobileNavDialog");
    opener = document.getElementById("mobileNavOpen");
    const sidebar = document.getElementById("siteNavigation");
    const shell = document.querySelector(".shell");
    const media = window.matchMedia("(max-width: 900px)");
    const settle = () => {
      opener.setAttribute("aria-expanded", "false");
      document.body.style.overflow = previousOverflow;
    };
    const place = () => {
      close();
      if (media.matches) dialog.appendChild(sidebar);
      else shell.prepend(sidebar);
    };
    opener.addEventListener("click", () => {
      if (!media.matches || dialog.open) return;
      previousOverflow = document.body.style.overflow;
      dialog.showModal();
      document.body.style.overflow = "hidden";
      opener.setAttribute("aria-expanded", "true");
    });
    document.getElementById("mobileNavClose").addEventListener("click", close);
    dialog.addEventListener("close", settle);
    dialog.addEventListener("click", event => {
      if (event.target !== dialog) return;
      const rect = dialog.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) close();
    });
    sidebar.addEventListener("click", event => {
      if (event.target.closest(".nav-item, .brand")) close();
    });
    media.addEventListener("change", place);
    place();
  }
  return {init, close};
})();
