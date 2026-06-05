# Crossroads MC — manifest management.
#
# Adding worlds/categories is just editing JSON under infra/manifests/ and running
# `cdk deploy`. These targets generate/edit those files for you.
#
#   make category CATEGORY=skyblock PORT=25568   # new slot (then add Cloudflare DNS)
#   make world CATEGORY=skyblock NAME="My World"  # new blank world → prints its UUID
#   make set CATEGORY=skyblock UUID=<uuid>         # point the slot at a world
#   make register-commands                         # (re)register Discord slash commands
#
# After `make category`/`make set`, run `cd infra && cdk deploy` to apply.

PYTHON ?= uv run python
MANIFEST_TOOL := manifest_tool.py
REGISTER_COMMANDS := register-commands.py

.PHONY: category world set register-commands

category:
	cd util && $(PYTHON) $(MANIFEST_TOOL) category --name "$(CATEGORY)" --port "$(PORT)" && cd ..

world:
	cd util && $(PYTHON) $(MANIFEST_TOOL) world --category "$(CATEGORY)" --name "$(NAME)" && cd ..

set:
	cd util && $(PYTHON) $(MANIFEST_TOOL) set --category "$(CATEGORY)" --uuid "$(UUID)" && cd ..

register-commands:
	cd util && $(PYTHON) $(REGISTER_COMMANDS) && cd ..
