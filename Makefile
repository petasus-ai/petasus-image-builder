SHELL := /usr/bin/env bash
.DEFAULT_GOAL := help
# One conversion at a time: each step works on output/$(NAME).
.NOTPARALLEL:

# The distribution's interpreter: python3-libguestfs is built for it only.
PYTHON := /usr/bin/python3
MIGRATE := bin/petasus-migrate
FIRMWARE_PACKAGE := $(if $(filter aarch64,$(shell uname -m)),edk2-aarch64,edk2-ovmf)
HOST_PACKAGES := guestfs-tools libguestfs-xfs python3-libguestfs python3-pyyaml $(FIRMWARE_PACKAGE) qemu-kvm qemu-img \
	podman xorriso dosfstools gdisk openssh-clients

export SRC SRC_FORMAT NAME OUTPUT TAG
export GENERALIZE NETWORK UEFI_CONVERT MISSING_MOUNTS BUILD_REPO_FILE EL_REPO_ISO SKIP_VERIFY
export REGISTRY REPOSITORY REGISTRY_AUTHFILE REGISTRY_CA SOURCE_REF DISTRO_TYPE
export VERIFY_TIMEOUT VERIFY_EXTRA GUEST_STEP_TIMEOUT NODE_EXPORTER_VERSION NODE_EXPORTER_TARBALL

##@ Helpers
.PHONY: help
help: ## Display this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m NAME=<image name> [SRC=<source disk>] [VARIABLE=value ...]\n"} \
		/^[a-zA-Z0-9_-]+:.*?##/ { printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2 } \
		/^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) }' $(MAKEFILE_LIST)

.PHONY: deps
deps: ## Install the host packages that are missing; refuses a transaction that upgrades installed ones
	@missing=(); for package in $(HOST_PACKAGES); do rpm -q "$$package" >/dev/null 2>&1 || missing+=("$$package"); done; \
	if [ $${#missing[@]} -eq 0 ]; then echo "every host package is installed"; exit 0; fi; \
	plan="$$(sudo dnf install --assumeno "$${missing[@]}" 2>&1 || true)"; \
	if grep -qE '^(Upgrading|Downgrading|Removing):' <<<"$$plan"; then \
		echo "$$plan"; echo "refusing: installing $${missing[*]} would change packages that are already installed" >&2; exit 1; \
	fi; \
	sudo dnf install -y "$${missing[@]}"

##@ Migration
.PHONY: inspect
inspect: ## Report what SRC holds and whether it converts; changes nothing
	$(MIGRATE) inspect

.PHONY: migrate
migrate: ## Convert SRC to output/NAME/disk.qcow2 and verify that it boots as the platform boots it
	$(MIGRATE) migrate

.PHONY: verify
verify: ## Boot output/NAME/disk.qcow2 again and check it
	$(MIGRATE) verify

.PHONY: package
package: ## Build the containerDisk image of the verified disk
	$(MIGRATE) package

.PHONY: push
push: ## Push the containerDisk image to REGISTRY/REPOSITORY:TAG and write output/NAME/image.json
	$(MIGRATE) push

.PHONY: all
all: migrate package push ## migrate, package and push

##@ Development
.PHONY: test
test: ## Run the unit tests and check the scripts' syntax
	$(PYTHON) -m unittest discover -s tests -v
	@for script in guest/*.sh; do sh -n "$$script" || exit 1; done
	@$(PYTHON) -m compileall -q pmig bin/petasus-migrate >/dev/null

.PHONY: clean
clean: ## Remove output/NAME, or every output when NAME is unset
	rm -rf -- "$${OUTPUT:-output}/$(NAME)"
