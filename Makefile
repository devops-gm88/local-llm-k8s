SHELL := /usr/bin/env bash
.DEFAULT_GOAL := help
REPO_ROOT := $(CURDIR)

.PHONY: help tools up deploy demo bench cost ci test manifests readme readme-check venv down stop destroy clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

tools: ## Download limactl, kubectl and kubeconform into .tools/ (no Homebrew, no sudo)
	./scripts/install_tools.sh

up: tools ## Create the Lima VM, install k3s, build and deploy everything
	./scripts/cluster_up.sh
	./scripts/deploy.sh

deploy: ## Build the gateway image and (re)apply manifests to a running cluster
	./scripts/deploy.sh

demo: ## Ask one question through the gateway (the acceptance proof)
	./scripts/demo.sh

bench: ## Run the 1/2/4-replica x 10/50-client benchmark and write bench/results.*
	./scripts/bench.sh

cost: ## Write cost/break_even.csv and the break-even chart from bench/results.json
	./scripts/cost.sh

readme: ## Re-render the README tables from the committed results
	.venv/bin/python bench/render_readme.py

ci: manifests test readme-check ## Everything CI runs that does not need Docker

readme-check: ## Fail if README.md no longer matches the committed results
	$(MAKE) --no-print-directory venv
	.venv/bin/python bench/render_readme.py --check

venv:
	@test -x .venv/bin/python || python3 -m venv .venv

manifests: ## Render both overlays and validate them with kubeconform
	./scripts/validate_manifests.sh

test: ## Run the gateway unit tests
	./scripts/test_gateway.sh

down: ## Delete the workloads, keep the VM
	./scripts/down.sh

stop: ## Shut the VM down
	./scripts/stop.sh

destroy: ## Delete the VM (asks first)
	./scripts/destroy.sh

clean: ## Remove generated results, charts and the venv
	rm -rf .venv bench/results.json bench/results.csv bench/charts cost/break_even.csv cost/break_even.png
