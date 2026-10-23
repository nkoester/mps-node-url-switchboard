# Installs the switchboard for the current user (default: ~/.local) plus a systemd user unit.
# Overridable: PREFIX, DESTDIR (staging root), SYSTEMD_USER_DIR, PYTHON.

NAME             := mps-node-url-switchboard
PREFIX           ?= $(HOME)/.local
BINDIR           ?= $(PREFIX)/bin
DOCDIR           ?= $(PREFIX)/share/doc/$(NAME)
SYSTEMD_USER_DIR ?= $(HOME)/.config/systemd/user
PYTHON           ?= python3

UNIT := $(NAME).service

.PHONY: all install uninstall test check-python

all: test

# The code needs Python 3.12+ (nested quotes in f-strings)
check-python:
	@$(PYTHON) -c 'import sys; sys.exit(sys.version_info < (3, 12))' || \
	  { echo "error: $(PYTHON) is older than 3.12" >&2; exit 1; }

install: check-python
	install -Dm755 mps_node_url_switchboard.py $(DESTDIR)$(BINDIR)/$(NAME)
	install -Dm644 README.md $(DESTDIR)$(DOCDIR)/README.md
	install -d $(DESTDIR)$(SYSTEMD_USER_DIR)
	sed -e 's|@BINDIR@|$(BINDIR)|g' -e 's|@DOCDIR@|$(DOCDIR)|g' systemd/$(UNIT) > $(DESTDIR)$(SYSTEMD_USER_DIR)/$(UNIT)
	chmod 644 $(DESTDIR)$(SYSTEMD_USER_DIR)/$(UNIT)
	@echo
	@echo "Installed $(BINDIR)/$(NAME) and $(SYSTEMD_USER_DIR)/$(UNIT)."
	@echo
	@echo "Enable the service (runs as your user, owns port 63320):"
	@echo "  systemctl --user daemon-reload"
	@echo "  systemctl --user enable --now $(NAME)"
	@echo "Start it at boot instead of at login:"
	@echo "  loginctl enable-linger \"\$$USER\""
	@echo "Already running? Pick up the new version:"
	@echo "  systemctl --user restart $(NAME)"
	@echo "Logs:"
	@echo "  journalctl --user -u $(NAME) -f"

uninstall:
	rm -f $(DESTDIR)$(BINDIR)/$(NAME) $(DESTDIR)$(SYSTEMD_USER_DIR)/$(UNIT)
	rm -rf $(DESTDIR)$(DOCDIR)
	@echo
	@echo "Removed the files. If the service was enabled, also run:"
	@echo "  systemctl --user disable --now $(NAME)"
	@echo "  systemctl --user daemon-reload"

test: check-python
	$(PYTHON) -m unittest discover -s tests
	@if command -v node >/dev/null; then \
	  tmp=$$(mktemp --suffix=.js) && \
	  $(PYTHON) -c 'import sys, mps_node_url_switchboard as m; open(sys.argv[1], "w").write(m._JS)' "$$tmp" && \
	  node --check "$$tmp" && echo "page JS: syntax ok"; status=$$?; rm -f "$$tmp"; exit $$status; \
	else echo "node not found: skipping the page JS syntax check"; fi
