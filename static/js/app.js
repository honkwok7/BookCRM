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
          if (!this.open) return;
          this.open = false;
          var body = document.getElementById("modal-body");
          if (body) body.innerHTML = "";
          document.body.dispatchEvent(new CustomEvent("modal:closed", { bubbles: true }));
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

  // Calendar (templates/calendar/): double-click an appointment to open it in the page dialog,
  // and drag a movable one to a new time (day and week views) or another day at the same time
  // (month view). A drop asks the server to
  // confirm (MoveView); closing the dialog without moving redraws the calendar, which puts the
  // appointment back. A click on an empty spot (from today on, with data-new-url) opens a
  // new appointment there. Handlers are delegated from the document because htmx replaces
  // #calendar on every navigation. Without JavaScript the links open the appointment page.
  (function () {
    var DRAG_THRESHOLD_PX = 6;
    var drag = null;
    var pendingMove = false;
    var suppressClick = false;
    var lastPointerType = "mouse";

    function openInDialog(url) {
      if (window.htmx) window.htmx.ajax("GET", url, { target: "#modal-body", swap: "innerHTML" });
    }

    function remPx() {
      return parseFloat(window.getComputedStyle(document.documentElement).fontSize) || 16;
    }

    function classNumber(el, prefix) {
      var match = el.className.match(new RegExp("(?:^|\\s)" + prefix + "(\\d+)(?:\\s|$)"));
      return match ? parseInt(match[1], 10) : 1;
    }

    function setClassNumber(el, prefix, value) {
      el.className = el.className.replace(new RegExp("(^|\\s)" + prefix + "\\d+(?=\\s|$)"), "");
      el.classList.add(prefix + value);
    }

    function pad(n) {
      return (n < 10 ? "0" : "") + n;
    }

    // Minutes after midnight for a grid row, and that time as the card shows it ("2:15").
    function rowMinutes(grid, row) {
      return (
        parseInt(grid.dataset.firstHour, 10) * 60 +
        (row - 1) * parseInt(grid.dataset.slotMinutes, 10)
      );
    }

    function clockLabel(minutes) {
      return (Math.floor(minutes / 60) % 12 || 12) + ":" + pad(minutes % 60);
    }

    function showTime(state, row) {
      if (state.timeEl) state.timeEl.textContent = clockLabel(rowMinutes(state.grid, row));
    }

    function restore(state) {
      if (state.chip) state.chip.className = state.chipClass;
      if (state.cell) state.cell.classList.remove("cal-drop-target");
      state.el.className = state.className;
      if (state.timeEl) state.timeEl.textContent = state.timeText;
      if (state.el.parentNode !== state.parent) state.parent.insertBefore(state.el, state.next);
    }

    function columnAt(x, y) {
      var hit = document.elementFromPoint(x, y);
      return hit ? hit.closest("[data-cal-column]") : null;
    }

    function rowAt(column, y, state) {
      var top = column.getBoundingClientRect().top;
      top += parseFloat(window.getComputedStyle(column).paddingTop) || 0;
      var row = Math.floor((y - top) / state.rowPx) + 1 - state.grabRow;
      return Math.max(1, Math.min(row, state.rows - state.span + 1));
    }

    // -- Team panel ---------------------------------------------------------------------------
    // The panel button hides or shows #cal-panel (remembered in this browser); "All team" ticks
    // or unticks everyone before the form's change event sends the filters; the search box
    // only hides names in the list (its change event must not send the form).

    var PANEL_KEY = "bookcrm.calendarPanel";

    function readPanel() {
      try {
        return window.localStorage.getItem(PANEL_KEY);
      } catch (error) {
        return null;
      }
    }

    function setPanel(state) {
      document.body.dataset.calPanel = state;
      try {
        window.localStorage.setItem(PANEL_KEY, state);
      } catch (error) {
        // private window: the choice lasts for this page only
      }
    }

    var savedPanel = readPanel();
    if (savedPanel === "open" || savedPanel === "closed") document.body.dataset.calPanel = savedPanel;

    function syncTeam(root) {
      var team = (root || document).querySelector("[data-cal-team]");
      if (!team) return;
      var all = team.querySelector("[data-cal-all]");
      var members = team.querySelectorAll("[data-cal-member]");
      var ticked = team.querySelectorAll("[data-cal-member]:checked").length;
      if (all) {
        all.checked = ticked === members.length;
        all.indeterminate = ticked > 0 && ticked < members.length;
      }
      filterTeam(team);
    }

    function filterTeam(team) {
      var search = team.querySelector("[data-cal-search]");
      var query = search ? search.value.trim().toLowerCase() : "";
      var shown = 0;
      team.querySelectorAll("[data-cal-member-row]").forEach(function (row) {
        var match = !query || row.dataset.name.indexOf(query) !== -1;
        row.hidden = !match;
        if (match) shown += 1;
      });
      var none = team.querySelector("[data-cal-no-match]");
      if (none) none.classList.toggle("hidden", shown > 0);
    }

    document.addEventListener("click", function (event) {
      var toggle = event.target.closest && event.target.closest("[data-cal-panel-toggle]");
      if (toggle) {
        var panel = document.getElementById("cal-panel");
        setPanel(panel && panel.offsetParent !== null ? "closed" : "open");
        return;
      }
      var all = event.target.closest && event.target.closest("[data-cal-all]");
      if (!all) return;
      // The click has already toggled the box; tick the members to match before "change".
      var team = all.closest("[data-cal-team]");
      team.querySelectorAll("[data-cal-member]").forEach(function (box) {
        box.checked = all.checked;
      });
    });

    document.addEventListener(
      "change",
      function (event) {
        if (event.target.matches && event.target.matches("[data-cal-search]")) {
          event.stopPropagation();
        }
      },
      true,
    );

    document.addEventListener("input", function (event) {
      if (!event.target.matches || !event.target.matches("[data-cal-search]")) return;
      filterTeam(event.target.closest("[data-cal-team]"));
    });

    document.addEventListener("htmx:afterSettle", function () {
      syncTeam();
    });
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", function () {
        syncTeam();
      });
    } else {
      syncTeam();
    }

    // -- New appointment from an empty spot --------------------------------------------------

    var ghost = null;

    // The bookable column under the pointer (not an appointment, not a past day), or null.
    function emptyColumn(target) {
      if (!target.closest || target.closest("[data-open-url], a, button")) return null;
      var column = target.closest("#calendar [data-cal-column]");
      if (!column || column.dataset.past !== undefined) return null;
      return column.closest("[data-cal-grid][data-new-url]") ? column : null;
    }

    function clickedRow(column, y) {
      var grid = column.closest("[data-cal-grid]");
      var rows = parseInt(grid.dataset.rows, 10);
      return rowAt(column, y, { rowPx: remPx(), grabRow: 0, rows: rows, span: 1 });
    }

    function newAppointment(host, date, minutes, staff) {
      var url = host.dataset.newUrl + "?date=" + encodeURIComponent(date);
      if (minutes !== null) url += "&at=" + pad(Math.floor(minutes / 60)) + ":" + pad(minutes % 60);
      if (staff) url += "&staff=" + encodeURIComponent(staff);
      if (host.dataset.location) url += "&location=" + encodeURIComponent(host.dataset.location);
      openInDialog(url);
    }

    function hideGhost() {
      if (ghost && ghost.parentNode) ghost.parentNode.removeChild(ghost);
    }

    document.addEventListener("pointermove", function (event) {
      if (drag || event.pointerType === "touch") return hideGhost();
      var column = emptyColumn(event.target);
      if (!column) return hideGhost();
      var grid = column.closest("[data-cal-grid]");
      var row = Math.min(clickedRow(column, event.clientY), parseInt(grid.dataset.rows, 10) - 1);
      if (!ghost) {
        ghost = document.createElement("div");
        ghost.setAttribute("aria-hidden", "true");
      }
      ghost.className = "cal-ghost col-span-full row-span-2 row-start-" + row;
      ghost.textContent = "+ " + clockLabel(rowMinutes(grid, row));
      if (ghost.parentNode !== column) column.appendChild(ghost);
    });

    document.addEventListener("click", function (event) {
      if (suppressClick || event.defaultPrevented || event.button !== 0) return;
      var column = emptyColumn(event.target);
      if (column) {
        var grid = column.closest("[data-cal-grid]");
        var minutes = rowMinutes(grid, clickedRow(column, event.clientY));
        hideGhost();
        newAppointment(grid, column.dataset.date, minutes, column.dataset.staff || grid.dataset.staff);
        return;
      }
      if (!event.target.closest || event.target.closest("a, button")) return;
      var cell = event.target.closest("#calendar [data-cal-day]");
      var month = cell && cell.closest("[data-cal-month][data-new-url]");
      if (!month || cell.dataset.past !== undefined) return;
      newAppointment(month, cell.dataset.date, null, month.dataset.staff);
    });

    // -- Drag and drop -----------------------------------------------------------------------

    document.addEventListener("pointerdown", function (event) {
      lastPointerType = event.pointerType || "mouse";
      var el = event.target.closest && event.target.closest(".cal-movable[data-move-url]");
      // Touch scrolls the calendar instead; touch users move appointments with Reschedule.
      if (!el || event.button !== 0 || event.pointerType === "touch") return;
      var month = el.closest("[data-cal-month]");
      if (month) {
        // Month view: the chip's list item moves between day cells; the time stays the same.
        var item = el.closest("li");
        drag = {
          el: item,
          chip: el,
          chipClass: el.className,
          month: month,
          startX: event.clientX,
          startY: event.clientY,
          started: false,
          className: item.className,
          parent: item.parentNode,
          next: item.nextSibling,
          startCell: el.closest("[data-cal-day]"),
          cell: null,
        };
        return;
      }
      var grid = el.closest("[data-cal-grid]");
      if (!grid) return;
      var rowPx = remPx();
      drag = {
        el: el,
        grid: grid,
        startX: event.clientX,
        startY: event.clientY,
        started: false,
        className: el.className,
        parent: el.parentNode,
        next: el.nextSibling,
        rowPx: rowPx,
        rows: parseInt(grid.dataset.rows, 10),
        span: classNumber(el, "row-span-"),
        startRow: classNumber(el, "row-start-"),
        grabRow: Math.floor((event.clientY - el.getBoundingClientRect().top) / rowPx),
        timeEl: el.querySelector(".font-semibold"),
      };
      drag.timeText = drag.timeEl ? drag.timeEl.textContent : "";
    });

    document.addEventListener("pointermove", function (event) {
      if (!drag) return;
      if (!drag.started) {
        var dx = event.clientX - drag.startX;
        var dy = event.clientY - drag.startY;
        if (Math.sqrt(dx * dx + dy * dy) < DRAG_THRESHOLD_PX) return;
        drag.started = true;
        document.body.classList.add("cal-drag-active");
        if (drag.month) {
          drag.chip.classList.add("cal-dragging");
        } else {
          drag.el.classList.add("cal-dragging", "col-span-full");
          setClassNumber(drag.el, "col-start-", 1);
        }
      }
      event.preventDefault();
      if (drag.month) {
        var hit = document.elementFromPoint(event.clientX, event.clientY);
        var cell = hit ? hit.closest("[data-cal-day]") : null;
        if (!cell || !drag.month.contains(cell) || cell === drag.cell) return;
        if (drag.cell) drag.cell.classList.remove("cal-drop-target");
        drag.cell = cell;
        cell.classList.add("cal-drop-target");
        cell.querySelector("ul").appendChild(drag.el);
        return;
      }
      var column =
        drag.grid.dataset.sameColumn !== undefined
          ? drag.parent
          : columnAt(event.clientX, event.clientY) || drag.el.parentNode;
      if (!column || !drag.grid.contains(column)) return;
      if (drag.el.parentNode !== column) column.appendChild(drag.el);
      var row = rowAt(column, event.clientY, drag);
      setClassNumber(drag.el, "row-start-", row);
      showTime(drag, row);
    });

    function endDrag(cancelled) {
      var state = drag;
      drag = null;
      if (!state || !state.started) return;
      document.body.classList.remove("cal-drag-active");
      // Swallow the click that ends the drag (it may land on another element, so don't wait).
      suppressClick = true;
      window.setTimeout(function () {
        suppressClick = false;
      }, 0);
      if (state.month) {
        var day = state.cell || state.startCell;
        if (cancelled || day === state.startCell) {
          restore(state);
          return;
        }
        day.classList.remove("cal-drop-target");
        state.chip.classList.remove("cal-dragging");
        state.chip.classList.add("cal-pending");
        pendingMove = true;
        openInDialog(
          state.chip.dataset.moveUrl +
            "?start=" +
            encodeURIComponent(day.dataset.date + "T" + state.chip.dataset.time) +
            "&tz=" +
            encodeURIComponent(state.month.dataset.zone),
        );
        return;
      }
      var column = state.el.parentNode;
      var row = classNumber(state.el, "row-start-");
      if (cancelled || (column === state.parent && row === state.startRow)) {
        restore(state);
        return;
      }
      state.el.classList.remove("cal-dragging");
      state.el.classList.add("cal-pending");
      var minutes = rowMinutes(state.grid, row);
      var start = column.dataset.date + "T" + pad(Math.floor(minutes / 60)) + ":" + pad(minutes % 60);
      pendingMove = true;
      openInDialog(
        state.el.dataset.moveUrl +
          "?start=" +
          encodeURIComponent(start) +
          "&tz=" +
          encodeURIComponent(state.grid.dataset.zone),
      );
    }

    document.addEventListener("pointerup", function () {
      endDrag(false);
    });
    document.addEventListener("pointercancel", function () {
      endDrag(true);
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && drag) endDrag(true);
    });

    // A single click (mouse) does nothing, so a double-click can open the dialog without the
    // link navigating. Keyboard activation (Enter) and a tap on a touch screen open it at once.
    // Ctrl/Cmd/Shift-click still opens the appointment page in a new tab or window.
    document.addEventListener("click", function (event) {
      var link = event.target.closest && event.target.closest("#calendar [data-open-url]");
      if (!link) return;
      if (suppressClick) {
        event.preventDefault();
        return;
      }
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      if (event.detail === 0 || lastPointerType === "touch") openInDialog(link.dataset.openUrl);
    });

    document.addEventListener("dblclick", function (event) {
      var link = event.target.closest && event.target.closest("#calendar [data-open-url]");
      if (!link) return;
      event.preventDefault();
      openInDialog(link.dataset.openUrl);
    });

    // The server redraws the calendar after a move (calendar-refresh). If the dialog closes
    // without one, redraw anyway so a dropped appointment goes back to its real time.
    document.addEventListener("calendar-refresh", function () {
      pendingMove = false;
    });
    document.addEventListener("modal:closed", function () {
      if (!pendingMove) return;
      pendingMove = false;
      document.body.dispatchEvent(new CustomEvent("calendar-refresh"));
    });
  })();

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
