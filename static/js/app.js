/* BookCRM UI behaviour.
 *
 * Alpine runs as its CSP build, so behaviour lives in these registered components instead of
 * inline JavaScript. Loaded before Alpine (both deferred, in order), so the components are
 * registered when Alpine starts.
 */
(function () {
  "use strict";

  var TOAST_TIMEOUT_MS = 5000;

  document.addEventListener("alpine:init", function () {
    var Alpine = window.Alpine;

    // Off-canvas sidebar on small screens.
    Alpine.data("shell", function () {
      return {
        sidebarOpen: false,
        openSidebar: function () {
          this.sidebarOpen = true;
        },
        closeSidebar: function () {
          this.sidebarOpen = false;
        },
      };
    });

    // Menu button + panel. Closes on outside click and Escape; focus returns to the button.
    Alpine.data("dropdown", function () {
      return {
        open: false,
        toggle: function () {
          this.open = !this.open;
        },
        close: function (focusButton) {
          if (!this.open) return;
          this.open = false;
          if (focusButton && this.$refs.button) this.$refs.button.focus();
        },
      };
    });

    // One dialog per page (#modal). Content is loaded into #modal-body by htmx
    // (hx-target="#modal-body"); the dialog opens after the swap. The server closes it by
    // sending the "modal:close" trigger (HX-Trigger header).
    Alpine.data("modal", function () {
      return {
        open: false,
        init: function () {
          var self = this;
          this.$el.addEventListener("htmx:afterSwap", function (event) {
            if (event.target.id === "modal-body" && event.target.innerHTML.trim()) self.show();
          });
          document.body.addEventListener("modal:close", function () {
            self.close();
          });
        },
        show: function () {
          this.open = true;
        },
        close: function () {
          this.open = false;
          var body = document.getElementById("modal-body");
          if (body) body.innerHTML = "";
        },
      };
    });

    // Toast stack. Server messages arrive as JSON in #initial-toasts (Django messages) or as
    // a "toast" event: HX-Trigger: {"toast": {"message": "...", "level": "success"}}.
    Alpine.data("toasts", function () {
      return {
        items: [],
        nextId: 1,
        init: function () {
          var self = this;
          var initial = document.getElementById("initial-toasts");
          if (initial) {
            JSON.parse(initial.textContent).forEach(function (toast) {
              self.add(toast);
            });
          }
          document.body.addEventListener("toast", function (event) {
            self.add(event.detail || {});
          });
        },
        add: function (toast) {
          var self = this;
          var item = {
            id: this.nextId++,
            message: String(toast.message || ""),
            level: toast.level || "info",
          };
          if (!item.message) return;
          this.items.push(item);
          window.setTimeout(function () {
            self.dismiss(item.id);
          }, TOAST_TIMEOUT_MS);
        },
        dismiss: function (id) {
          this.items = this.items.filter(function (item) {
            return item.id !== id;
          });
        },
        classFor: function (item) {
          return "alert-" + (item.level === "error" ? "error" : item.level);
        },
      };
    });

    // Client-side tabs. Server-driven tabs are plain links (components/tabs.html).
    Alpine.data("tabs", function (initial) {
      return {
        active: initial,
        select: function (name) {
          this.active = name;
        },
        isActive: function (name) {
          return this.active === name;
        },
      };
    });
  });

  // htmx does not swap error responses; tell the user something went wrong instead of
  // failing silently.
  document.addEventListener("htmx:responseError", function (event) {
    var status = event.detail.xhr ? event.detail.xhr.status : 0;
    var message =
      status === 403
        ? "You don't have permission to do that."
        : status === 404
          ? "That item no longer exists."
          : status === 429
            ? "Too many requests. Please wait a moment and try again."
            : "Something went wrong. Please try again.";
    document.body.dispatchEvent(
      new CustomEvent("toast", { detail: { message: message, level: "error" } }),
    );
  });

  document.addEventListener("htmx:sendError", function () {
    document.body.dispatchEvent(
      new CustomEvent("toast", {
        detail: { message: "Can't reach the server. Check your connection.", level: "error" },
      }),
    );
  });
})();
